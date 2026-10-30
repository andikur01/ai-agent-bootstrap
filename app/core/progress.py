"""Confirmation gate + progress reporting.

Heavy tasks (image/video/web/deploy/research) cost money and time, so they never
start silently. The flow is:

    gate.ask()  -> user taps ✅/❌ inline button or replies "ya"/"tidak"
    gate.await_decision() -> True/False, with a timeout that cancels by default

Progress reporting is deliberately throttled: editing a Telegram message more
often than ~1.2s gets rate-limited and looks like a bug to the user. Milestone
screenshots bypass the throttle because they carry new information.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.core.errors import TaskCancelledError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

YES_WORDS = {"ya", "iya", "yes", "y", "gas", "lanjut", "oke", "ok", "boleh", "setuju", "sip"}
NO_WORDS = {"tidak", "ga", "gak", "engga", "no", "n", "batal", "cancel", "stop", "jangan"}


def parse_decision(text: str) -> bool | None:
    word = text.strip().lower().split()[0] if text.strip() else ""
    word = word.strip(".,!?")
    if word in YES_WORDS:
        return True
    if word in NO_WORDS:
        return False
    return None


@dataclass
class PendingConfirmation:
    user_id: int
    route_intent: str
    args: dict
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


class ConfirmationGate:
    """Tracks one pending confirmation per user."""

    def __init__(self, timeout_seconds: int = 180):
        self.timeout_seconds = timeout_seconds
        self._pending: dict[int, PendingConfirmation] = {}

    def create(self, user_id: int, route_intent: str, args: dict) -> PendingConfirmation:
        loop = asyncio.get_running_loop()
        pending = PendingConfirmation(
            user_id=user_id,
            route_intent=route_intent,
            args=dict(args),
            future=loop.create_future(),
        )
        self._pending[user_id] = pending
        return pending

    def get(self, user_id: int) -> PendingConfirmation | None:
        return self._pending.get(user_id)

    def resolve(self, user_id: int, approved: bool) -> bool:
        pending = self._pending.pop(user_id, None)
        if pending is None:
            return False
        if not pending.future.done():
            pending.future.set_result(approved)
        return True

    def cancel(self, user_id: int) -> None:
        pending = self._pending.pop(user_id, None)
        if pending and not pending.future.done():
            pending.future.set_result(False)

    async def wait(self, pending: PendingConfirmation) -> bool:
        try:
            return await asyncio.wait_for(pending.future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            self._pending.pop(pending.user_id, None)
            log.info("confirmation timed out user=%s", pending.user_id)
            return False

    @property
    def pending_users(self) -> list[int]:
        return list(self._pending)


@dataclass
class ProgressUpdate:
    percent: int
    message: str
    screenshot: str = ""
    force: bool = False


class ProgressReporter:
    """Throttled progress rendering to a single Telegram message."""

    MIN_INTERVAL = 1.2
    HEARTBEAT_SECONDS = 8.0

    def __init__(
        self,
        send: Callable[[str], Awaitable[object]],
        *,
        title: str = "Memproses",
        bar_width: int = 12,
        send_photo: Callable[[str, str], Awaitable[object]] | None = None,
    ):
        self.send = send
        self.send_photo = send_photo
        self.title = title
        self.bar_width = bar_width
        self._last_sent = 0.0
        self._last_percent = -1
        self._message = None
        self._lock = asyncio.Lock()

    @staticmethod
    def render_bar(percent: int, width: int = 12) -> str:
        percent = max(0, min(100, percent))
        filled = round(width * percent / 100)
        return "█" * filled + "░" * (width - filled)

    def render(self, update: ProgressUpdate) -> str:
        bar = self.render_bar(update.percent, self.bar_width)
        return f"*{self.title}*\n`{bar}` {update.percent}%\n{update.message}"

    async def update(
        self, percent: int, message: str, *, screenshot: str = "", force: bool = False
    ) -> None:
        async with self._lock:
            now = time.monotonic()
            percent = max(0, min(100, int(percent)))
            moved = percent - self._last_percent >= 5 or percent in (0, 100)
            elapsed = now - self._last_sent

            # Send when: forced, carrying a screenshot, a meaningful jump that
            # also respects the rate limit, or a heartbeat so long tasks do not
            # look frozen.
            if not force and not screenshot:
                if not moved and elapsed < self.HEARTBEAT_SECONDS:
                    return
                if moved and elapsed < self.MIN_INTERVAL:
                    return

            self._last_percent = percent
            self._last_sent = now
            text = self.render(ProgressUpdate(percent, message))

            if screenshot and self.send_photo:
                try:
                    await self.send_photo(screenshot, text)
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning("progress photo failed, falling back to text: %s", exc)

            try:
                if self._message is None:
                    self._message = await self.send(text)
                else:
                    await self._message.edit_text(text)
            except Exception as exc:  # noqa: BLE001 - never fail the task over progress
                log.debug("progress update skipped: %s", exc)

    async def done(self, message: str) -> None:
        await self.update(100, message, force=True)

    async def fail(self, message: str) -> None:
        await self.update(self._last_percent if self._last_percent > 0 else 0, f"❌ {message}", force=True)


class CancellationFlag:
    """Cooperative cancellation checked by long-running tool loops."""

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""

    def cancel(self, reason: str = "user") -> None:
        self._cancelled = True
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise TaskCancelledError(f"dibatalkan ({self._reason})")
