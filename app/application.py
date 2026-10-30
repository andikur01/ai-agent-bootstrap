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
            await self.telegram_app.start()
            await self.telegram_app.updater.start_polling(
                drop_pending_updates=True, allowed_updates=["message", "callback_query"]
            )
        log.info("application started")

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
        .build()
    )

    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("bantu", bot.cmd_help))
    app.add_handler(CommandHandler("belajar", bot.cmd_belajar))
    app.add_handler(CommandHandler("pikir", bot.cmd_pikir))
    app.add_handler(CommandHandler("memori", bot.cmd_memori))
    app.add_handler(CommandHandler("feedback", bot.cmd_feedback))
    app.add_handler(CommandHandler("batal", bot.cmd_batal))
    app.add_handler(CommandHandler("jadwal", bot.cmd_jadwal))
    app.add_handler(CommandHandler("bataljadwal", bot.cmd_bataljadwal))
    app.add_handler(CommandHandler("admin", bot.cmd_admin))
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^confirm:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL,
            bot.on_message,
        )
    )
    return app
