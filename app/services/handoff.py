"""Hand a question to the user mid-task and continue once they answer.

Some steps cannot be automated and must not be faked: a one-time code sent to
the user's email, a captcha, a security prompt on their phone. The honest
design is to stop at that point, ask, and resume from where the task paused -
not to fail the whole task and make the user start over, and not to pretend
the step succeeded.

This is deliberately a small registry rather than a queue: at most one question
per user is outstanding, and the next message that arrives from that user is
either the answer or a fresh instruction. If the user ignores the question, the
waiter times out and the task reports that it needs them, instead of hanging.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.core.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class PendingAnswer:
    user_id: int
    question: str
    future: asyncio.Future = field(repr=False)


class HandoffService:
    """Ask the user for one piece of information and wait for it."""

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = max(30, timeout_seconds)
        self._pending: dict[int, PendingAnswer] = {}

    def pending(self, user_id: int) -> PendingAnswer | None:
        return self._pending.get(user_id)

    def ask_nowait(self, user_id: int, question: str) -> PendingAnswer:
        """Register the question without blocking, so the caller can send it.

        The caller (the bot) needs to send the question through Telegram before
        it starts waiting, and sending is itself async. Registering first keeps
        a very fast reply from arriving before the slot exists.
        """
        existing = self._pending.get(user_id)
        if existing is not None and not existing.future.done():
            return existing
        pending = PendingAnswer(
            user_id=user_id, question=question, future=asyncio.get_running_loop().create_future()
        )
        self._pending[user_id] = pending
        return pending

    async def wait(self, pending: PendingAnswer) -> str | None:
        """Return the user's answer, or None when they did not reply in time."""
        try:
            return await asyncio.wait_for(pending.future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            log.info("handoff timed out for user %s", pending.user_id)
            return None
        finally:
            if self._pending.get(pending.user_id) is pending:
                self._pending.pop(pending.user_id, None)

    def answer(self, user_id: int, text: str) -> bool:
        """Deliver a message to a waiting task. True when it was consumed.

        Returning False is what lets an ordinary message keep its normal route:
        only a user who is actually being asked loses the message to the waiter.
        """
        pending = self._pending.get(user_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(text)
        return True

    def cancel(self, user_id: int) -> None:
        pending = self._pending.pop(user_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(None)
