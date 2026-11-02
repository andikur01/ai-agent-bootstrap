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
from app.llm.key_pool import build_key_pool
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
            # drop_pending_updates must stay False. With True, every message the
            # user sent while the bot was down is deleted on startup - which is
            # exactly what happened: they wrote from their phone, got silence,
            # and the message was gone before the bot came back. A short backlog
            # after a restart is far better than losing the message entirely.
            await self.telegram_app.updater.start_polling(
                drop_pending_updates=False, allowed_updates=["message", "callback_query"]
            )
            await self._announce_startup()
        log.info("application started")

    async def _announce_startup(self) -> None:
        """Tell the user the bot is up, so silence is never ambiguous.

        A bot that restarts silently looks identical to a bot that is down. One
        short message per start removes that doubt, and it doubles as proof that
        the restart-on-crash unit is actually working.
        """
        settings = self.settings
        if not getattr(settings, "notify_on_start", True):
            return
        for user_id in settings.telegram_allowed_user_ids[:3]:
            try:
                await self.telegram_app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🟢 *Bot hidup lagi.*\n\n"
                        "Pesan yang kamu kirim waktu bot mati tetap aku terima — "
                        "aku balas sekarang.\n\n"
                        "Cek cepat: `/status` atau `/stok`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:  # noqa: BLE001 - a failed notice is not fatal
                log.debug("startup notice to %s failed: %s", user_id, exc)

    async def _publish_commands(self) -> None:
        """Register the command list so Telegram shows its native menu button."""
        from telegram import BotCommand

        commands = [
            BotCommand("menu", "Menu semua fitur"),
            BotCommand("status", "Status bot & API"),
            BotCommand("tes", "Tes diri sendiri: sandbox + isolasi"),
            BotCommand("status_model", "Model AI yang aktif"),
            BotCommand("pikir", "Analisis mendalam"),
            BotCommand("belajar", "Cari & simpan pengetahuan"),
            BotCommand("memori", "Statistik memori & token"),
            BotCommand("keys", "Key pool: status, add, auto, sumber"),
            BotCommand("login", "Login sekali ke situs provider"),
            BotCommand("live", "Lihat & kendalikan layar browser AI"),
            BotCommand("buat_gambar", "Generate gambar"),
            BotCommand("edit_gambar", "Edit gambar terakhir"),
            BotCommand("buat_video", "Buat video"),
            BotCommand("edit_video", "Edit video terakhir"),
            BotCommand("text_to_voice", "Teks jadi suara"),
            BotCommand("buat_web", "Buat website static"),
            BotCommand("buat_web_full", "Buat website fullstack"),
            BotCommand("perbaiki_web", "Perbaiki website"),
            BotCommand("browse", "Buka situs & operasikan"),
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
    key_pool = build_key_pool(settings, state_file=settings.data_path / "key_pool.json")
    gateway = LLMGateway(settings, budget=budget, key_pool=key_pool)
    memory = MemoryManager(store, vectors, registry, settings, gateway=gateway)
    cache = SemanticCache(
        store, embedder, enabled=settings.semantic_cache_enabled
    )
    await cache.init()

    # Built before the orchestrator so the browser agent can be given the saved
    # session and the shared display: without them it can only read public pages.
    from app.services.account_vault import build_vault
    from app.services.browser_session import build_profiles
    from app.services.key_harvester import build_harvester
    from app.services.live_view import build_live_view

    profiles = build_profiles(settings)
    live_view = build_live_view(settings)
    vault = build_vault(settings)

    orchestrator = Orchestrator(
        settings=settings,
        store=store,
        gateway=gateway,
        memory=memory,
        cache=cache,
        budget=budget,
        profiles=profiles,
        vault=vault,
        live_view=live_view,
    )

    async def notifier(chat_id: int, text: str) -> None:
        if telegram_app is None:
            log.info("reminder (no telegram): %s", text)
            return
        await telegram_app.bot.send_message(chat_id=chat_id, text=text)

    scheduler = SchedulerService(store, settings, notifier=notifier)
    orchestrator.scheduler = scheduler

    from app.services.sandbox import build_sandbox

    orchestrator.sandbox = build_sandbox(settings)

    from app.services.key_discovery import build_discovery

    discovery = build_discovery(
        settings,
        profiles=profiles,
        vault=vault,
        live_view=live_view,
        outputs_dir=settings.outputs_path,
    )
    harvester = build_harvester(
        settings, profiles=profiles, outputs_dir=settings.outputs_path, vault=vault
    )

    # Bring the shared display up at boot rather than on first use. The user
    # asked for a browser they can watch; a screen that only appears once a task
    # happens to need it is a screen they cannot open while waiting. Failure
    # here is logged, never fatal - the bot runs fine without a display.
    try:
        state = await live_view.ensure()
        if state.running:
            log.info(
                "live view ready: display=%s viewer=%s",
                state.display,
                live_view.viewer_url() or "(LIVE_VIEW_HOST belum diset)",
            )
        else:
            log.warning("live view unavailable: %s", "; ".join(state.errors) or "unknown")
    except Exception as exc:  # noqa: BLE001 - a display must not stop the bot
        log.warning("live view failed to start: %s", exc)

    # Start the site host once at boot. Binding per deploy would race when two
    # sites are built at the same time, and a port that opens only after a
    # request is a port that is closed when the first link is clicked.
    if settings.public_base_url:
        try:
            from app.services.hosting_service import SiteHost

            site_host = SiteHost(
                settings.outputs_path / "sites",
                port=settings.site_host_port,
                public_base_url=settings.public_base_url,
            )
            if await site_host.start():
                orchestrator.deployer.site_host = site_host
                log.info("site host ready on port %s", settings.site_host_port)
            else:
                log.warning("site host could not bind port %s", settings.site_host_port)
        except Exception as exc:  # noqa: BLE001 - hosting must not stop the bot
            log.warning("site host failed to start: %s", exc)

    bot = TelegramBot(
        settings=settings,
        store=store,
        orchestrator=orchestrator,
        scheduler=scheduler,
        profiles=profiles,
        harvester=harvester,
        discovery=discovery,
        live_view=live_view,
        vault=vault,
    )

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
    app.add_handler(CommandHandler("browse", bot.cmd_browse))
    app.add_handler(CommandHandler("keys", bot.cmd_keys))
    app.add_handler(CommandHandler("stok", bot.cmd_stok))
    app.add_handler(CommandHandler("disk", bot.cmd_disk))
    app.add_handler(CommandHandler("tes", bot.cmd_tes))
    app.add_handler(CommandHandler("akun", bot.cmd_akun))
    app.add_handler(CommandHandler("login", bot.cmd_login))
    app.add_handler(CommandHandler("live", bot.cmd_live))
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
