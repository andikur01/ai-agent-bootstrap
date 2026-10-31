"""Telegram presentation helpers: sending media, milestones, and screenshots.

Kept separate from the handlers so the Telegram API details (file size limits,
caption limits, photo-vs-document decisions) live in one place.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.logging_setup import get_logger
from app.core.utils import human_bytes
from app.security.redact import assert_no_secrets, redact

log = get_logger(__name__)

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".ogg", ".wav", ".opus"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split long text on paragraph/sentence boundaries, never mid-word."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_text(message, text: str, *, reply_markup=None) -> None:
    """Send text safely: redacted, split, with Markdown fallback to plain."""
    safe = redact(assert_no_secrets(text, "chat message"))
    for index, chunk in enumerate(split_message(safe)):
        markup = reply_markup if index == len(split_message(safe)) - 1 else None
        try:
            await message.reply_text(chunk, parse_mode="Markdown", reply_markup=markup)
        except Exception:  # noqa: BLE001 - malformed markdown is the common cause
            try:
                await message.reply_text(chunk, reply_markup=markup)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to send text: %s", exc)
                return


async def send_file(message, path: str | Path, *, caption: str = "") -> bool:
    """Send a produced file with the right Telegram method and size handling."""
    path = Path(path)
    if not path.exists():
        log.warning("send_file: missing %s", path)
        return False

    size = path.stat().st_size
    limit = 45 * 1024 * 1024  # bot API upload ceiling for non-premium bots
    safe_caption = redact(caption)[:TELEGRAM_CAPTION_LIMIT] if caption else ""

    try:
        if size > limit:
            await message.reply_text(
                f"File terlalu besar untuk Telegram ({human_bytes(size)}). "
                f"Aku simpan di: `{path}`"
            )
            return False

        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            try:
                with path.open("rb") as handle:
                    await message.reply_photo(handle, caption=safe_caption)
                return True
            except Exception as exc:  # noqa: BLE001 - some PNGs fail photo validation
                log.info("reply_photo failed (%s); sending as document", exc)

        if suffix in VIDEO_SUFFIXES:
            with path.open("rb") as handle:
                await message.reply_video(handle, caption=safe_caption, supports_streaming=True)
            return True

        if suffix in AUDIO_SUFFIXES:
            with path.open("rb") as handle:
                await message.reply_audio(handle, caption=safe_caption)
            return True

        with path.open("rb") as handle:
            await message.reply_document(handle, caption=safe_caption)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("send_file failed for %s: %s", path, exc)
        try:
            await message.reply_text(f"Gagal kirim `{path.name}`: {str(exc)[:120]}")
        except Exception:  # noqa: BLE001
            pass
        return False


async def send_media_group(message, paths: list[str], *, caption: str = "") -> None:
    """Send up to 10 images in one album; falls back to individual sends."""
    from telegram import InputMediaPhoto

    existing = [Path(p) for p in paths if Path(p).exists()][:10]
    if not existing:
        return
    if len(existing) == 1:
        await send_file(message, existing[0], caption=caption)
        return
    try:
        media = []
        for index, path in enumerate(existing):
            with path.open("rb") as handle:
                data = handle.read()
            media.append(
                InputMediaPhoto(data, caption=caption if index == 0 else None)
            )
        await message.reply_media_group(media)
    except Exception as exc:  # noqa: BLE001
        log.info("media group failed (%s); sending individually", exc)
        for path in existing:
            await send_file(message, path)


async def send_screenshots(message, paths: list[str], *, caption: str = "") -> None:
    """Milestone screenshots for QA evidence, with secret regions blurred.

    Screenshots can capture env panels, devtools, or dashboards, so every
    outbound screenshot goes through the masking guard before it leaves the
    process. The guard is heuristic (it blurs the header strip), which is why we
    also never capture secrets in the first place.
    """
    from app.core.config import get_settings
    from app.services.browser_qa import mask_secrets_in_image

    secrets = [v for v in get_settings().secret_values() if len(v) >= 12]
    existing = [p for p in paths if Path(p).exists()][:4]
    for index, path in enumerate(existing):
        if secrets:
            path = mask_secrets_in_image(path, secrets)
        await send_file(message, path, caption=caption if index == 0 else "")


async def download_telegram_file(bot, file_id: str, target_dir: Path, *, suffix: str = "") -> Path:
    """Download an incoming photo/voice/document to local storage."""
    target_dir.mkdir(parents=True, exist_ok=True)
    telegram_file = await bot.get_file(file_id)
    name = Path(telegram_file.file_path or "upload").name
    if suffix and not name.endswith(suffix):
        name = f"{Path(name).stem}{suffix}"
    target = target_dir / f"{_unique_prefix()}-{name}"
    await telegram_file.download_to_drive(custom_path=str(target))
    return target


def _unique_prefix() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H%M%S-%f")[:12]


class TypingIndicator:
    """Keeps Telegram's "typing..." alive during long work."""

    def __init__(self, chat, *, interval: float = 4.0):
        self.chat = chat
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        try:
            while True:
                await self.chat.send_action("typing")
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
