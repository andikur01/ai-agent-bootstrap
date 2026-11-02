"""Entrypoint: `python -m app.main` or `python main.py`."""
from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import sys
from pathlib import Path

from app.application import build_app
from app.core.config import get_settings
from app.core.logging_setup import get_logger, setup_logging


def _take_single_instance_lock(settings) -> "os._wrap_close | None":
    """Refuse to start when another copy of the bot already owns the token.

    Two processes polling the same Telegram token do not both work - they
    cancel each other out with ``Conflict: terminated by other getUpdates
    request``, and the user sees a bot that receives messages and answers
    none. That is exactly what happened here, so the second copy now exits
    with a clear reason instead of fighting the first one.

    ``flock`` is used rather than a pid file because the kernel drops the lock
    the moment the process dies, including on a crash or an OOM kill, so a
    stale lock can never block a legitimate restart.
    """
    path = Path(getattr(settings, "data_path", Path("data"))) / "bot.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")  # noqa: SIM115 - held for the life of the process
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


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

    lock = _take_single_instance_lock(settings)
    if lock is None:
        log.error(
            "Bot lain sudah jalan dengan token ini. Dua bot tidak bisa jalan "
            "bersama - Telegram hanya mengizinkan satu. Matikan yang lama dulu."
        )
        return 4

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
    except Exception as exc:  # noqa: BLE001 - top-level UX guard
        from app.security.redact import redact

        name = type(exc).__name__
        if name == "InvalidToken":
            print(
                "TELEGRAM_BOT_TOKEN ditolak Telegram (401 Unauthorized).\n"
                "Token salah, sudah di-revoke, atau sudah direset di @BotFather.\n"
                "Ambil token baru dari BotFather lalu update .env, kemudian jalankan lagi.",
                file=sys.stderr,
            )
            return 3
        if name in {"MissingSecretError", "ProviderUnavailableError"}:
            print(redact(f"Konfigurasi belum lengkap: {exc}"), file=sys.stderr)
            return 3
        print(redact(f"Gagal start: {name}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
