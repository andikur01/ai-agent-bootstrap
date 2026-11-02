"""Live feed: let the user watch the agent work, inside the chat.

Why not a real video stream: a Telegram bot cannot start a live video call or
stream into a chat. There is no such API - only users can go live. So "siaran
langsung" has to be built out of what a bot *can* do, and the honest version is
a motion feed: the screen frames the agent is already taking are stitched into a
short animated GIF and posted on an interval. The user sees the browser move,
not a single frozen still.

Two things make this worth having rather than spam:

* One message, updated in place. Editing the same message's media means the feed
  is a single tile that changes, instead of a wall of new photos.
* Frames only go out when there is something new. A quiet stretch produces no
  message at all.

The noVNC live view still exists for real control (typing a captcha yourself).
This is the passive "I can see it working" half, which is what was asked for.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_INTERVAL = 6.0
DEFAULT_MAX_FRAMES = 8
DEFAULT_WIDTH = 640
FRAME_DURATION_MS = 500


def build_gif(
    frames: list[str | Path],
    out_path: str | Path,
    *,
    width: int = DEFAULT_WIDTH,
    duration_ms: int = FRAME_DURATION_MS,
) -> str | None:
    """Stitch screenshots into one animated GIF. Returns None if it cannot.

    Returns None rather than raising: a missing Pillow, or a frame that is not a
    readable image, must never fail the task the user is actually waiting on.
    """
    if not frames:
        return None
    try:
        from PIL import Image
    except ImportError:
        log.debug("Pillow not installed, no live feed")
        return None

    images = []
    for frame in frames:
        try:
            image = Image.open(frame)
            image.load()
        except Exception as exc:  # noqa: BLE001 - one bad frame is not fatal
            log.debug("skipping unreadable frame %s: %s", frame, exc)
            continue
        image = image.convert("RGB")
        if image.width > width:
            height = round(image.height * width / image.width)
            image = image.resize((width, height))
        images.append(image)

    if not images:
        return None

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        images[0].save(
            out,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not build live feed gif: %s", exc)
        return None
    return str(out)


class LiveFeed:
    """Posts the agent's screen to the chat as a moving, self-updating tile."""

    def __init__(
        self,
        *,
        send_animation,
        edit_animation=None,
        send_photo=None,
        outputs_dir: str | Path,
        interval: float = DEFAULT_INTERVAL,
        max_frames: int = DEFAULT_MAX_FRAMES,
        width: int = DEFAULT_WIDTH,
    ):
        self.send_animation = send_animation
        self.edit_animation = edit_animation
        self.send_photo = send_photo
        self.outputs = Path(outputs_dir) / "live"
        self.interval = max(2.0, float(interval))
        self.max_frames = max(2, int(max_frames))
        self.width = width

        self._frames: deque[str] = deque(maxlen=self.max_frames)
        self._sent_count = 0
        self._message_id: int | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- public API
    def push(self, screenshot: str | Path | None) -> None:
        """Record a frame. Called from the task's progress hook."""
        if not screenshot:
            return
        path = Path(screenshot)
        if not path.exists():
            return
        self._frames.append(str(path))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval + 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # -------------------------------------------------------------- internals
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return  # stopped before the next tick
            except asyncio.TimeoutError:
                pass
            try:
                await self._publish()
            except Exception as exc:  # noqa: BLE001 - the feed must never break a task
                log.debug("live feed publish failed: %s", exc)

    async def _publish(self) -> None:
        async with self._lock:
            frames = list(self._frames)
        # Nothing new since the last post: stay quiet rather than repeat a frame.
        if len(frames) <= self._sent_count:
            return
        self._sent_count = len(frames)

        gif = build_gif(frames, self.outputs / f"feed-{int(time.time())}.gif", width=self.width)
        if gif is None:
            return

        caption = "🔴 Sedang kerja - ini layarnya, bergerak sendiri"
        try:
            if self._message_id is not None and self.edit_animation is not None:
                await self.edit_animation(self._message_id, gif, caption)
                return
        except Exception as exc:  # noqa: BLE001 - fall through to a new message
            log.debug("editing live feed message failed: %s", exc)

        try:
            message = await self.send_animation(gif, caption)
            self._message_id = getattr(message, "message_id", None)
        except Exception as exc:  # noqa: BLE001
            log.debug("sending live feed animation failed: %s", exc)
