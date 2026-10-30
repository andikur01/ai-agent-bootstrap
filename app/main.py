"""Entrypoint: `python -m app.main` or `python main.py`."""
from __future__ import annotations

import asyncio
import signal
import sys

from app.application import build_app
from app.core.config import get_settings
from app.core.logging_setup import get_logger, setup_logging


async def run() -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    log = get_logger("app.main")

    if not settings.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN belum diisi. Copy .env.example ke .env dan isi tokennya.")
        return 2
    if not settings.telegram_allowed_user_ids:
        log.error(
            "TELEGRAM_ALLOWED_USER_IDS kosong. Bot sengaja menolak semua user sampai "
            "allow-list diisi (fail-closed)."
        )
        return 2

    app = await build_app(settings)
    stop_event = asyncio.Event()

    def _handle_signal(*_args) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover - windows
            signal.signal(sig, _handle_signal)

    await app.start()
    log.info("bot is running. Tekan Ctrl+C untuk berhenti.")
    try:
        await stop_event.wait()
    finally:
        await app.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
