"""Application wiring: build every component once and own the lifecycle.

``build_app`` is the single place where dependencies are constructed, which is
what makes the whole system testable - tests can build the same graph with a
stubbed gateway and no Telegram connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.bot.handlers import TelegramBot
from app.core.config import Settings, get_settings
from app.core.logging_setup import get_logger, setup_logging
from app.core.orchestrator import Orchestrator
from app.db.store import Store
from app.llm.budget import BudgetGuard
from app.llm.cache import SemanticCache
from app.llm.gateway import LLMGateway
from app.memory.artifacts import ArtifactRegistry
from app.memory.embeddings import build_embedder
from app.memory.manager import MemoryManager
from app.memory.vector_store import build_vector_store
from app.services.scheduler_service import SchedulerService

log = get_logger(__name__)


@dataclass
class Application:
    settings: Settings
    store: Store
    gateway: LLMGateway
    budget: BudgetGuard
    cache: SemanticCache
    memory: MemoryManager
    orchestrator: Orchestrator
    scheduler: SchedulerService
    bot: TelegramBot
    telegram_app: Any = None

    async def start(self) -> None:
        await self.scheduler.start()
        if self.telegram_app is not None:
            await self.telegram_app.initialize()
            await self._publish_commands()
            await self.telegram_app.start()
            await self.telegram_app.updater.start_polling(
                drop_pending_updates=True, allowed_updates=["message", "callback_query"]
            )
        log.info("application started")

    async def _publish_commands(self) -> None:
        """Register the command list so Telegram shows its native menu button."""
        from telegram import BotCommand

        commands = [
            BotCommand("menu", "Menu semua fitur"),
            BotCommand("status", "Status bot & API"),
            BotCommand("status_model", "Model AI yang aktif"),
            BotCommand("pikir", "Analisis mendalam"),
            BotCommand("belajar", "Cari & simpan pengetahuan"),
            BotCommand("memori", "Statistik memori & token"),
            BotCommand("buat_gambar", "Generate gambar"),
            BotCommand("edit_gambar", "Edit gambar terakhir"),
            BotCommand("buat_video", "Buat video"),
            BotCommand("edit_video", "Edit video terakhir"),
            BotCommand("text_to_voice", "Teks jadi suara"),
            BotCommand("buat_web", "Buat website static"),
            BotCommand("buat_web_full", "Buat website fullstack"),
            BotCommand("perbaiki_web", "Perbaiki website"),
            BotCommand("buat_n8n", "Buat workflow n8n"),
            BotCommand("ingat", "Set reminder"),
            BotCommand("jadwal", "Lihat jadwal"),
            BotCommand("baca_link", "Baca & rangkum link"),
            BotCommand("help", "Bantuan lengkap"),
        ]
        try:
            await self.telegram_app.bot.set_my_commands(commands)
        except Exception as exc:  # noqa: BLE001 - cosmetic, must never block startup
            log.warning("set_my_commands failed: %s", exc)

    async def stop(self) -> None:
        await self.scheduler.shutdown()
        if self.telegram_app is not None:
            try:
                await self.telegram_app.updater.stop()
                await self.telegram_app.stop()
                await self.telegram_app.shutdown()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram shutdown: %s", exc)
        self.store.close()
        log.info("application stopped")


async def build_app(settings: Settings | None = None, *, with_telegram: bool = True) -> Application:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)

    store = Store(settings.sqlite_path)
    embedder = build_embedder(settings)
    vectors = build_vector_store(settings, store, embedder)
    registry = ArtifactRegistry(store)
    budget = BudgetGuard(store, settings.daily_token_budget)
    gateway = LLMGateway(settings, budget=budget)
    memory = MemoryManager(store, vectors, registry, settings, gateway=gateway)
    cache = SemanticCache(
        store, embedder, enabled=settings.semantic_cache_enabled
    )
    await cache.init()

    orchestrator = Orchestrator(
        settings=settings,
        store=store,
        gateway=gateway,
        memory=memory,
        cache=cache,
        budget=budget,
    )

    async def notifier(chat_id: int, text: str) -> None:
        if telegram_app is None:
            log.info("reminder (no telegram): %s", text)
            return
        await telegram_app.bot.send_message(chat_id=chat_id, text=text)

    scheduler = SchedulerService(store, settings, notifier=notifier)
    orchestrator.scheduler = scheduler

    bot = TelegramBot(settings=settings, store=store, orchestrator=orchestrator, scheduler=scheduler)

    telegram_app = None
    if with_telegram and settings.telegram_bot_token:
        telegram_app = _build_telegram_app(settings, bot)
    elif with_telegram:
        log.warning("TELEGRAM_BOT_TOKEN kosong: bot tidak akan menerima pesan")

    log.info(
        "components ready: vector_backend=%s providers=%s cache=%s",
        getattr(vectors, "backend", "?"),
        ",".join(settings.available_providers()) or "none",
        settings.semantic_cache_enabled,
    )

    return Application(
        settings=settings,
        store=store,
        gateway=gateway,
        budget=budget,
        cache=cache,
        memory=memory,
        orchestrator=orchestrator,
        scheduler=scheduler,
        bot=bot,
        telegram_app=telegram_app,
    )


def _build_telegram_app(settings: Settings, bot: TelegramBot):
    from telegram.ext import (
        AIORateLimiter,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=3))
        .concurrent_updates(True)
    )
    if settings.telegram_api_base:
        app = app.base_url(settings.telegram_api_base)
    app = app.build()

    # Group -1: the authorisation gate must run before anything else.
    app.add_handler(
        MessageHandler(filters.ALL, bot.on_guard), group=-1
    )
    app.add_handler(CallbackQueryHandler(bot.on_guard), group=-1)

    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("bantu", bot.cmd_help))
    app.add_handler(CommandHandler("menu", bot.cmd_menu))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("status_model", bot.cmd_status_model))
    app.add_handler(CommandHandler("belajar", bot.cmd_belajar))
    app.add_handler(CommandHandler("pikir", bot.cmd_pikir))
    app.add_handler(CommandHandler("memori", bot.cmd_memori))
    app.add_handler(CommandHandler("feedback", bot.cmd_feedback))
    app.add_handler(CommandHandler("batal", bot.cmd_batal))
    app.add_handler(CommandHandler("jadwal", bot.cmd_jadwal))
    app.add_handler(CommandHandler("bataljadwal", bot.cmd_bataljadwal))
    app.add_handler(CommandHandler("admin", bot.cmd_admin))
    # Feature aliases, so the menu buttons have real commands behind them.
    app.add_handler(CommandHandler("buat_gambar", bot.cmd_buat_gambar))
    app.add_handler(CommandHandler("edit_gambar", bot.cmd_edit_gambar))
    app.add_handler(CommandHandler("buat_video", bot.cmd_buat_video))
    app.add_handler(CommandHandler("edit_video", bot.cmd_edit_video))
    app.add_handler(CommandHandler("buat_web", bot.cmd_buat_web))
    app.add_handler(CommandHandler("buat_web_full", bot.cmd_buat_web_full))
    app.add_handler(CommandHandler("perbaiki_web", bot.cmd_perbaiki_web))
    app.add_handler(CommandHandler("buat_n8n", bot.cmd_buat_n8n))
    app.add_handler(CommandHandler("ingat", bot.cmd_ingat))
    app.add_handler(CommandHandler("baca_link", bot.cmd_baca_link))
    app.add_handler(CommandHandler("voice_to_text", bot.cmd_voice_to_text))
    app.add_handler(CommandHandler("text_to_voice", bot.cmd_text_to_voice))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^(confirm|menu):"))
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL,
            bot.on_message,
        )
    )
    return app
