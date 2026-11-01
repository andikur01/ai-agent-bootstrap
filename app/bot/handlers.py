"""Telegram handlers: the only place that talks to the Telegram API.

Responsibilities kept here (and nowhere else):
* authorisation (allow-list, fail-closed)
* rate limiting and heavy-task quotas
* the confirmation gate with inline buttons
* progress reporting with milestone screenshots
* routing to the orchestrator and rendering the outcome
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.bot.presenter import (
    TypingIndicator,
    download_telegram_file,
    send_file,
    send_screenshots,
    send_text,
)
from app.core.errors import (
    AgentError,
    BudgetExceededError,
    MissingSecretError,
    RateLimitedError,
    TaskCancelledError,
    UnauthorizedUserError,
)
from app.core.intent import Intent, Route
from app.core.logging_setup import get_logger
from app.core.orchestrator import HELP_TEXT, Orchestrator
from app.core.progress import ConfirmationGate, ProgressReporter, parse_decision
from app.security.redact import redact

log = get_logger(__name__)

YES_CALLBACK = "confirm:yes"
NO_CALLBACK = "confirm:no"
MENU_PREFIX = "menu:"
# Action buttons inside the menu, e.g. "menu:ask:web" - a question, not a build.
ASK_PREFIX = "ask:"
# Tasks long enough that watching the screen is genuinely useful.
_WATCHABLE_INTENTS = {
    Intent.WEB_STATIC, Intent.WEB_FULLSTACK, Intent.WEB_REPAIR,
    Intent.VIDEO_CREATE, Intent.BROWSE,
}
_ASK_TEXTS = {
    "web": "apakah webnya sudah jadi?",
    "gambar": "apakah gambarnya sudah jadi?",
    "video": "apakah videonya sudah jadi?",
    "audio": "apakah audionya sudah jadi?",
    "n8n": "apakah workflow n8n-nya sudah jadi?",
}

# Section id -> (button label, body). Kept as plain data so the content can be
# tested without a Telegram connection.
#
# Command names are wrapped in backticks on purpose: send_text uses
# parse_mode="Markdown", where a bare "_" starts italics and Telegram silently
# swallows it, turning "/buat_gambar" into "/buatgambar" on the user's screen.
# Inside backticks the underscore is literal, so what the user reads is what
# they can type.
MENU_SECTIONS: dict[str, tuple[str, str]] = {
    "chat": (
        "💬 Chat & Thinking",
        "💬 *CHAT & THINKING*\n\n"
        "`/pikir` <topik> - Analisis mendalam 5 langkah\n"
        "`/belajar` <topik> - Cari di internet, rangkum, simpan permanen\n"
        "`/memori` - Statistik memori & token terpakai\n"
        "`/keys` - Status key pool & sisa quota\n"
        "`/feedback` <pesan> - Catat saranmu",
    ),
    "creative": (
        "🎨 Creative",
        "🎨 *CREATIVE*\n\n"
        "`/buat_gambar` <deskripsi> - Generate gambar AI\n"
        "`/edit_gambar` <deskripsi> - Edit gambar terakhir\n"
        "`/buat_video` <deskripsi> - Buat video animasi\n"
        "`/edit_video` <deskripsi> - Edit video terakhir\n"
        "`/text_to_voice` <teks> - Teks jadi suara\n"
        "`/voice_to_text` - Voice note jadi teks",
    ),
    "web": (
        "🌐 Web",
        "🌐 *WEB DEVELOPMENT*\n\n"
        "`/buat_web` <deskripsi> - Website static (dites dulu, lalu dikirim)\n"
        "`/buat_web_full` <deskripsi> - Website fullstack + login + database\n"
        "`/perbaiki_web` <link> - Perbaiki website yang error\n"
        "`/browse` <url> <tugas> - Buka situs & klik/isi form sendiri",
    ),
    "automation": (
        "⚙️ Automation",
        "⚙️ *AUTOMATION*\n\n"
        "`/buat_n8n` <deskripsi> - Buat workflow n8n\n"
        "`/ingat` <waktu> <pesan> - Set reminder\n"
        "`/jadwal` - Lihat semua jadwal\n"
        "`/bataljadwal` <id> - Batalkan satu jadwal",
    ),
    "link": (
        "🔗 Link & Media",
        "🔗 *LINK & MEDIA*\n\n"
        "`/baca_link` <url> - Baca & rangkum isi link\n"
        "Kirim link langsung - YouTube, TikTok, IG, artikel\n"
        "Kirim voice note - Langsung ditranskrip\n"
        "Kirim foto - Bisa langsung diedit",
    ),
    "browser": (
        "🖥️ Browser & Key",
        "🖥️ *BROWSER & KEY*\n\n"
        "`/live` - Lihat & kendalikan layar browser AI (dari HP)\n"
        "`/login` <url> - Login sekali ke situs provider\n"
        "`/keys` - Status key pool\n"
        "`/keys auto` <provider> - Aku ambil key sendiri\n"
        "`/keys add` <provider> <key> - Kamu kirim manual\n"
        "`/keys sumber` - Dari mana key bisa didapat\n"
        "`/browse` <url> <tugas> - Buka situs & kerjakan sendiri",
    ),
    "system": (
        "📊 System",
        "📊 *SYSTEM*\n\n"
        "`/status` - Status bot & provider API\n"
        "`/status_model` - Model AI yang aktif\n"
        "`/menu` - Tampilkan menu ini\n"
        "`/help` - Bantuan lengkap\n"
        "`/batal` - Batalkan konfirmasi",
    ),
}

MENU_TEXT = (
    "🤖 *AI Agent Menu*\n\n"
    "Klik tombol di bawah untuk lihat per kategori, atau langsung ketik\n"
    "perintahnya. Aku juga paham bahasa biasa - cukup bilang\n"
    "\"buatkan gambar kucing\", nggak wajib pakai perintah.\n\n"
    + "\n\n".join(body for _, body in MENU_SECTIONS.values())
    + "\n\n📊 `/status` - cek bot & API\n"
    "❓ `/help` - bantuan lengkap"
)


def menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"{MENU_PREFIX}{key}")
        for key, (label, _) in MENU_SECTIONS.items()
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("❓ Bantuan lengkap", callback_data=f"{MENU_PREFIX}help")])
    return InlineKeyboardMarkup(rows)


def menu_section(section: str) -> tuple[str, InlineKeyboardMarkup]:
    """Text + keyboard for one menu section, with a back button."""
    if section == "help":
        return HELP_TEXT, menu_keyboard()
    entry = MENU_SECTIONS.get(section)
    body = entry[1] if entry else MENU_TEXT
    rows = []
    # One-tap "is it done?" buttons. The user asked for exactly this: ask about
    # past work without the bot mistaking it for a new job.
    ask_map = {
        "web": [("🌐 Web terakhir?", "web")],
        "creative": [("🖼️ Gambar terakhir?", "gambar"), ("🎬 Video terakhir?", "video")],
        "automation": [("⚙️ Workflow terakhir?", "n8n")],
    }
    for label, key in ask_map.get(section, []):
        rows.append([InlineKeyboardButton(label, callback_data=f"{MENU_PREFIX}{ASK_PREFIX}{key}")])
    rows.append([InlineKeyboardButton("⬅️ Kembali ke menu", callback_data=f"{MENU_PREFIX}home")])
    return body, InlineKeyboardMarkup(rows)


def _args_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args) if context.args else ""



class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(self, user_id: int) -> None:
        now = time.monotonic()
        window = self._hits[user_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            raise RateLimitedError()
        window.append(now)


class TelegramBot:
    def __init__(
        self,
        *,
        settings,
        store,
        orchestrator: Orchestrator,
        scheduler=None,
        profiles=None,
        harvester=None,
        live_view=None,
    ):
        self.settings = settings
        self.store = store
        self.orch = orchestrator
        self.scheduler = scheduler
        self.profiles = profiles
        self.harvester = harvester
        self.live_view = live_view
        self.gate = ConfirmationGate(timeout_seconds=180)
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self._running: dict[int, asyncio.Task] = {}

    @property
    def gateway(self):
        return getattr(self.orch, "gateway", None)

    # ------------------------------------------------------------ authorisation
    def _authorise(self, user_id: int) -> None:
        allowed = self.settings.telegram_allowed_user_ids
        if not allowed:
            # Fail closed: an unconfigured bot must not be usable by strangers.
            raise UnauthorizedUserError(
                "Bot belum dikonfigurasi: isi TELEGRAM_ALLOWED_USER_IDS di .env "
                f"(user id kamu: {user_id})."
            )
        if user_id not in allowed:
            raise UnauthorizedUserError()

    def _is_admin(self, user_id: int) -> bool:
        admins = self.settings.telegram_admin_user_ids
        return bool(admins) and user_id in admins

    # ------------------------------------------------------------------ entry
    async def on_guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Authorise before any other handler runs.

        Command handlers and callbacks do not pass through ``on_message``, so
        without this a stranger could drive /jadwal, /admin and every feature
        command. Runs in group -1 and stops propagation for unauthorised users.
        """
        user = update.effective_user
        if user is None:
            return
        message = update.effective_message
        text = (getattr(message, "text", "") or "").strip().lower()
        # /start is the onboarding path: it reports the caller's user id so they
        # can be added to the allow-list. cmd_start authorises on its own.
        if text.startswith("/start"):
            return
        try:
            self._authorise(user.id)
        except UnauthorizedUserError as exc:
            target = message
            if target is not None:
                await target.reply_text(exc.user_message)
            elif update.callback_query is not None:
                await update.callback_query.answer("Tidak diizinkan.", show_alert=False)
            log.warning("unauthorised access attempt user=%s", user.id)
            raise ApplicationHandlerStop from None

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        try:
            self._authorise(user.id)
        except UnauthorizedUserError as exc:
            await message.reply_text(exc.user_message)
            log.warning("unauthorised access attempt user=%s", user.id)
            return

        await self.store.upsert_user(user.id, user.username or "", user.first_name or "")

        text = (message.text or message.caption or "").strip()

        # A pending confirmation intercepts the next message.
        pending = self.gate.get(user.id)
        if pending is not None:
            decision = parse_decision(text)
            if decision is None and not text:
                await message.reply_text("Aku masih menunggu konfirmasi: balas *ya* atau *tidak*.")
                return
            approved = bool(decision)
            self.gate.resolve(user.id, approved)
            if not approved:
                await message.reply_text("Oke, aku batalkan.")
                return
            route = Route(
                Intent(pending.route_intent),
                dict(pending.args),
                confidence=1.0,
                source="confirmed",
            )
            await self._run_task(update, context, route, confirmed=True)
            return

        try:
            self.limiter.check(user.id)
        except RateLimitedError as exc:
            await message.reply_text(exc.user_message)
            return

        photo_path = ""
        voice_path = ""
        document_path = ""
        if message.photo:
            photo_path = str(
                await download_telegram_file(
                    context.bot, message.photo[-1].file_id, self.settings.data_path / "uploads", suffix=".jpg"
                )
            )
        if message.voice or message.audio:
            media = message.voice or message.audio
            suffix = ".ogg" if message.voice else (Path(media.file_name or "audio.mp3").suffix or ".mp3")
            voice_path = str(
                await download_telegram_file(
                    context.bot, media.file_id, self.settings.data_path / "uploads", suffix=suffix
                )
            )
        if message.document:
            document_path = str(
                await download_telegram_file(
                    context.bot,
                    message.document.file_id,
                    self.settings.data_path / "uploads",
                    suffix=Path(message.document.file_name or "").suffix,
                )
            )

        if not text and not (photo_path or voice_path or document_path):
            await message.reply_text("Aku menerima pesan kosong. Coba tulis sesuatu atau kirim file.")
            return

        # Remember the raw turn first, so nothing is lost if routing fails.
        await self.orch.memory.remember_message(
            user.id,
            "user",
            text or "[media]",
            {"photo": bool(photo_path), "voice": bool(voice_path), "document": bool(document_path)},
        )

        route = await self.orch.classify(
            user.id,
            text,
            has_photo=bool(photo_path),
            has_voice=bool(voice_path),
            has_document=bool(document_path),
        )

        # A slash-word that matched no command and no rule would otherwise be
        # treated as free chat, so a typo like "/buatgambar" burned an LLM call
        # and returned a provider error. Say so instead, and show the menu.
        if text.startswith("/") and route.source == "fallback":
            await send_text(
                message,
                f"Command `{text.split()[0]}` tidak ada.\n\n"
                "Cek `/menu` untuk daftar lengkap, atau tulis maksudmu pakai\n"
                "bahasa biasa (contoh: \"buatkan gambar kucing\").",
                reply_markup=menu_keyboard(),
            )
            return

        route.args.update(
            {
                "photo_path": photo_path,
                "voice_path": voice_path,
                "document_path": document_path,
            }
        )

        if route.needs_confirm and not route.args.get("confirmed"):
            await self._ask_confirmation(message, user.id, route)
            return

        await self._run_task(update, context, route)

    # ---------------------------------------------------------- confirmation
    async def _ask_confirmation(self, message, user_id: int, route: Route) -> None:
        # attach the resolved reference so the prompt can name the target
        reference = await self.orch.memory.resolve_reference(
            user_id, str(route.args.get("text") or "")
        )
        if reference.artifact is not None:
            route.args["reference_brief"] = reference.artifact.brief()
        elif reference.needs_clarification:
            kind = reference.kind or "pekerjaan itu"
            await message.reply_text(
                f"Aku belum punya {kind} sebelumnya, jadi tidak ada yang bisa aku edit.\n"
                f"Mau aku buat {kind} baru?"
            )
            return

        self.gate.create(user_id, route.intent.value, route.args)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Ya, lanjut", callback_data=YES_CALLBACK),
                    InlineKeyboardButton("❌ Tidak", callback_data=NO_CALLBACK),
                ]
            ]
        )
        await message.reply_text(
            self.orch.confirmation_prompt(route), parse_mode="Markdown", reply_markup=keyboard
        )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        try:
            self._authorise(query.from_user.id)
        except UnauthorizedUserError:
            await query.answer("Tidak diizinkan.")
            return

        # Menu buttons are navigation, not task confirmation - handle and return.
        if (query.data or "").startswith(MENU_PREFIX):
            payload = query.data[len(MENU_PREFIX) :]
            # "ask:web" style buttons ask a question about existing work, so the
            # user gets an answer from one tap instead of typing a sentence.
            if payload.startswith(ASK_PREFIX):
                await query.answer()
                question = payload[len(ASK_PREFIX) :]
                await self._run_confirmed(
                    update, context, Intent.CHAT, {"text": _ASK_TEXTS.get(question, question)}
                )
                return
            body, keyboard = menu_section(payload)
            await query.answer()
            try:
                await query.edit_message_text(redact(body), parse_mode="Markdown", reply_markup=keyboard)
            except Exception:  # noqa: BLE001 - unchanged content / markdown edge cases
                await query.edit_message_text(redact(body), reply_markup=keyboard)
            return

        user_id = query.from_user.id
        pending = self.gate.get(user_id)
        approved = query.data == YES_CALLBACK
        await query.answer("Oke!" if approved else "Dibatalkan")

        if pending is None:
            await query.edit_message_text("Konfirmasi ini sudah kedaluwarsa.")
            return
        self.gate.resolve(user_id, approved)
        if not approved:
            await query.edit_message_text("Dibatalkan. 👍")
            return

        await query.edit_message_text("Lanjut ✅")
        route = Route(
            Intent(pending.route_intent), dict(pending.args), confidence=1.0, source="confirmed"
        )
        await self._run_task(update, context, route, confirmed=True)

    # ----------------------------------------------------------------- runner
    async def _run_task(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, route: Route, *, confirmed: bool = False
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        user_id = user.id

        # heavy quota
        if route.intent in {Intent.WEB_STATIC, Intent.WEB_FULLSTACK, Intent.WEB_REPAIR, Intent.VIDEO_CREATE}:
            used = await self.store.count_tasks_today(user_id, route.intent.value)
            if used >= self.settings.heavy_task_daily_limit:
                await message.reply_text(
                    f"Kuota tugas berat hari ini sudah habis ({used}/{self.settings.heavy_task_daily_limit}). "
                    "Coba lagi besok atau naikkan `HEAVY_TASK_DAILY_LIMIT`."
                )
                return

        task_id = await self.store.create_task(
            user_id, route.intent.value, {"text": str(route.args.get("text") or "")[:500]}
        )

        # Heavy work gets a screen. The user asked to *see* progress, and these
        # are the tasks with real waiting in them: the QA browser then renders on
        # this display, so /live shows the site being tested while it happens.
        if route.intent in _WATCHABLE_INTENTS and self.live_view is not None:
            try:
                state = await self.live_view.ensure()
                url = self.live_view.viewer_url() if state.running else ""
                if url:
                    await message.reply_text(
                        "🖥️ Kamu bisa nonton prosesnya langsung:\n"
                        f"{url}\nPassword: `{state.password}`",
                        parse_mode="Markdown",
                    )
            except Exception as exc:  # noqa: BLE001 - never block the task
                log.debug("live view not started for task: %s", exc)

        reporter = ProgressReporter(
            lambda text: message.reply_text(text, parse_mode="Markdown"),
            title=f"{route.intent.value}",
            send_photo=lambda path, caption: message.reply_photo(
                Path(path).open("rb"), caption=redact(caption)[:1024]
            ),
        )

        async def progress(percent: int, note: str, *, screenshot: str = "") -> None:
            await reporter.update(percent, note, screenshot=screenshot)

        try:
            async with TypingIndicator(message.chat):
                await progress(5, "Mulai...")
                outcome = await self.orch.execute(
                    user_id,
                    route,
                    progress=progress,
                    photo_path=str(route.args.get("photo_path") or ""),
                    voice_path=str(route.args.get("voice_path") or ""),
                    document_path=str(route.args.get("document_path") or ""),
                    chat_id=message.chat_id,
                )
            await self.store.update_task(task_id, "done", outcome.text[:500])

            # Milestone screenshots first: evidence before the summary.
            if outcome.milestone_screenshots:
                await send_screenshots(message, outcome.milestone_screenshots, caption="Screenshot hasil QA")

            body = outcome.text
            if outcome.cached:
                body += "\n\n_(dari cache, hemat token)_"
            await send_text(message, body)

            for path in outcome.files or []:
                await send_file(message, path)

            await self.orch.memory.remember_message(user_id, "assistant", outcome.text[:2000])

        except TaskCancelledError as exc:
            await self.store.update_task(task_id, "cancelled")
            await message.reply_text(f"Dibatalkan: {exc}")
        except MissingSecretError as exc:
            await self.store.update_task(task_id, "blocked")
            await send_text(message, exc.user_message)
            await self.store.log_error(route.intent.value, f"missing secret {exc.name}", user_id)
        except BudgetExceededError as exc:
            await self.store.update_task(task_id, "blocked")
            await send_text(message, exc.user_message)
        except AgentError as exc:
            await self.store.update_task(task_id, "failed", str(exc))
            hint = self._capacity_hint()
            await send_text(
                message,
                f"❌ {exc.user_message}\n\nDetail: {str(exc)[:300]}" + hint,
            )
        except Exception as exc:  # noqa: BLE001
            await self.store.update_task(task_id, "failed", str(exc))
            await self.store.log_error(route.intent.value, f"{type(exc).__name__}: {exc}", user_id)
            log.exception("task crashed intent=%s", route.intent.value)
            await send_text(message, "❌ Ada error tak terduga. Sudah aku catat di log.")

    def _capacity_hint(self) -> str:
        """Tell the user where to get a key when every provider is exhausted.

        A bare "all providers failed" leaves the user stuck. If the key pool
        knows which providers are fully benched, name them and hand over the
        exact console URL, so the fix is one click plus `/keys auto`.
        """
        pool = getattr(self.gateway, "key_pool", None)
        if pool is None:
            return ""
        try:
            spent = pool.exhausted()
        except Exception:  # noqa: BLE001 - a hint must never break error handling
            return ""
        if not spent:
            return ""
        from app.llm.credentials import PROVIDERS

        lines = ["", "🔑 *Key-nya sedang habis semua:*"]
        for name in spent:
            provider = PROVIDERS.get(name)
            if not provider:
                continue
            ready = pool.soonest_ready(name)
            lines.append(
                f"• {provider.label} — siap lagi ~{ready}s\n"
                f"  Tambah sekarang: `/keys auto {name}` atau `/keys add {name} <key>`"
            )
        lines.append("")
        lines.append("Tambah key baru langsung dipakai, tanpa restart.")
        return "\n".join(lines)

    # --------------------------------------------------------- commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user:
            try:
                self._authorise(user.id)
            except UnauthorizedUserError as exc:
                await update.effective_message.reply_text(exc.user_message)
                return
            await self.store.upsert_user(user.id, user.username or "", user.first_name or "")
        await send_text(update.effective_message, HELP_TEXT)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await send_text(update.effective_message, HELP_TEXT)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await send_text(update.effective_message, MENU_TEXT, reply_markup=menu_keyboard())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        providers = self.settings.available_providers()
        if not providers:
            providers = ["tidak ada (isi minimal 1 API key LLM)"]
        lines = [
            "📊 *Status bot*",
            f"Bot: aktif ✅",
            f"Provider LLM: {', '.join(providers)}",
            f"Model aktif: `{self.settings.small_model}` (kecil) / `{self.settings.big_model}` (besar)",
            f"Vector memory: {'chroma' if self.settings.chroma_host else 'sqlite'}",
            f"Embedding: `{self.settings.embedding_model}` ({self.settings.embedding_dim}d)",
            f"Cache semantik: {'aktif' if self.settings.semantic_cache_enabled else 'mati'}",
            f"Scheduler: {'aktif' if self.scheduler is not None else 'mati'}",
        ]

        # Real numbers, not vibes: the user asked to see progress and usage.
        user = update.effective_user
        if user is not None:
            budget = getattr(self.orch, "budget", None)
            if budget is not None:
                try:
                    used = await budget.usage(user.id)
                    left = await budget.remaining(user.id)
                    lines.append(
                        f"Token hari ini: {used['total_tokens']:,} terpakai, "
                        f"{left:,} sisa"
                    )
                except Exception:  # noqa: BLE001 - status must never crash
                    pass
            store = getattr(self, "store", None)
            if store is not None:
                try:
                    tasks = await store.task_counts(user.id)
                    lines.append(
                        f"Tugas: {tasks.get('done', 0)} selesai, "
                        f"{tasks.get('failed', 0)} gagal, "
                        f"{tasks.get('running', 0)} jalan"
                    )
                except Exception:  # noqa: BLE001
                    pass

        pool = getattr(self.gateway, "key_pool", None)
        if pool is not None:
            try:
                total_keys = sum(len(v) for v in pool.states.values())
                spent = pool.exhausted()
                lines.append(f"Key pool: {total_keys} key aktif")
                if spent:
                    lines.append(f"⚠️ Key habis: {', '.join(spent)}")
            except Exception:  # noqa: BLE001
                pass

        await send_text(update.effective_message, "\n".join(lines))

    async def cmd_status_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from app.llm.gateway import TIER_MODELS

        providers = self.settings.available_providers()
        lines = ["🧠 *Model AI aktif*", f"Urutan fallback: {', '.join(providers) or 'kosong'}"]
        if not providers:
            lines.append("\nBelum ada provider siap. Isi GROQ_API_KEY di .env.")
        for tier in ("small", "big"):
            lines.append(f"\n*Tier {tier}:*")
            for provider in providers:
                model = TIER_MODELS.get(tier, {}).get(provider, "-")
                lines.append(f"- {provider}: `{model}`")
        lines.append("\nModel kecil dipakai untuk chat/klasifikasi; model besar untuk tugas berat.")
        await send_text(update.effective_message, "\n".join(lines))

    async def cmd_ingat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        body = " ".join(context.args) if context.args else ""
        await self._run_confirmed(update, context, Intent.REMINDER, {"text": body})

    async def cmd_baca_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        body = " ".join(context.args) if context.args else ""
        await self._run_confirmed(update, context, Intent.LINK_INGEST, {"text": body})

    async def cmd_voice_to_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.STT, {})

    async def cmd_text_to_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        body = " ".join(context.args) if context.args else ""
        await self._run_confirmed(update, context, Intent.TTS, {"text": body})

    async def cmd_buat_gambar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.IMAGE_CREATE, {"text": _args_text(context)})

    async def cmd_edit_gambar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.IMAGE_EDIT, {"text": _args_text(context)})

    async def cmd_buat_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.VIDEO_CREATE, {"text": _args_text(context)})

    async def cmd_edit_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.VIDEO_EDIT, {"text": _args_text(context)})

    async def cmd_buat_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.WEB_STATIC, {"text": _args_text(context)})

    async def cmd_buat_web_full(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.WEB_FULLSTACK, {"text": _args_text(context)})

    async def cmd_perbaiki_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.WEB_REPAIR, {"text": _args_text(context)})

    async def cmd_buat_n8n(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.N8N, {"text": _args_text(context)})

    async def cmd_browse(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.BROWSE, {"text": _args_text(context)})


    async def cmd_belajar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        topic = " ".join(context.args) if context.args else ""
        if not topic:
            await update.effective_message.reply_text(
                "Mau belajar tentang apa? Contoh: /belajar retrieval augmented generation"
            )
            return
        await self._run_confirmed(update, context, Intent.LEARN, {"text": f"belajar {topic}"})

    async def cmd_pikir(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        question = " ".join(context.args) if context.args else ""
        if not question:
            await update.effective_message.reply_text("Pikir tentang apa? Contoh: /pikir kenapa langit biru")
            return
        await self._run_confirmed(update, context, Intent.THINK, {"text": question})

    async def cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the agent's screen as a link the user can watch and click.

        This is the Astra/Argon trick, done with free parts: Xvfb is the screen,
        x11vnc exports it, noVNC turns it into a web page. The user opens one URL
        and sees exactly what the browser sees - and can take over for a captcha
        or a login.
        """
        user_id = update.effective_user.id
        self._authorise(user_id)
        if self.live_view is None:
            await update.message.reply_text("Live view belum aktif di server ini.")
            return

        status = await update.message.reply_text("🖥️ Menyiapkan layar browser...")
        state = await self.live_view.ensure()

        if not state.running:
            await status.edit_text(
                "❌ Live view gagal jalan:\n"
                + "\n".join(f"• {e}" for e in state.errors)
                + "\n\nDi server tanpa layar, ini memang tidak bisa. "
                "Pakai `/keys add <provider> <key>` untuk key-nya."
            )
            return

        url = self.live_view.viewer_url()
        if not url:
            await status.edit_text(
                "Layar jalan, tapi URL publiknya belum diset.\n"
                "Isi `LIVE_VIEW_HOST=https://work-2-....prod-runtime.all-hands.dev` "
                "di `.env` lalu `/live` lagi.",
                parse_mode="Markdown",
            )
            return

        await status.edit_text(
            "🖥️ *Layar browser AI*\n\n"
            f"👉 {url}\n\n"
            f"Password VNC: `{state.password}`\n\n"
            "Buka linknya di HP. Kamu bisa *lihat* dan *klik* langsung. "
            "Aku tetap yang menjalankan tugas; kalau perlu captcha atau login, "
            "kamu ambil alih di layar itu.\n\n"
            "Catatan: siapa pun yang punya link + password ini bisa mengendalikan "
            "browser. Jangan dibagikan.",
            parse_mode="Markdown",
        )

    async def cmd_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open a real browser so the user signs in once; we keep the session.

        The password is never sent to the bot. The user types it into the browser
        window (or watches progress screenshots on a headless server), and only
        the resulting cookies are stored, in the gitignored data/ directory.
        """
        user_id = update.effective_user.id
        self._authorise(user_id)
        args = list(context.args or [])
        if not args:
            await update.message.reply_text(
                "Pakai: `/login <url>`\n\n"
                "Contoh:\n"
                "`/login https://console.groq.com/keys`\n"
                "`/login https://aistudio.google.com/apikey`\n\n"
                "Aku buka browsernya, kamu login di situ. Password tidak "
                "pernah lewat aku. Setelah itu `/keys auto <provider>` bisa "
                "ambil key sendiri.",
                parse_mode="Markdown",
            )
            return

        url = args[0]
        if not url.startswith("http"):
            url = "https://" + url
        if self.harvester is None:
            await update.message.reply_text("Browser belum siap di server ini.")
            return

        from app.services.browser_session import host_from_url

        host = host_from_url(url)
        if self.profiles is not None:
            self.profiles.permissions.remember(host, True)

        seconds = int(getattr(self.settings, "browser_login_seconds", 240))
        status = await update.message.reply_text(
            f"🌐 Membuka browser di {host}...\n\n"
            f"Login di jendela itu. Aku tunggu sampai {seconds} detik dan "
            "kirim screenshot kalau perlu."
        )

        async def report(pct: int, text: str, screenshot=None) -> None:
            try:
                await status.edit_text(f"🌐 {pct}% — {text}")
            except Exception:  # noqa: BLE001 - edits can be rate-limited
                pass

        result = await self.harvester.open_for_login(
            url, user_id=user_id, seconds=seconds, progress=report
        )

        for shot in result.screenshots[-2:]:
            try:
                with open(shot, "rb") as handle:
                    await update.message.reply_photo(handle)
            except Exception:  # noqa: BLE001
                pass

        icon = "✅" if result.ok else "⚠️"
        await update.message.reply_text(
            f"{icon} {result.message}\n\n"
            f"Kalau sudah: `/keys auto <provider>`",
            parse_mode="Markdown",
        )

    async def cmd_keys_auto(self, update: Update, raw: str) -> None:
        """Fetch a new key from a console the user is already signed into."""
        from app.llm.credentials import PROVIDERS, resolve_provider
        from app.services.browser_session import host_from_url

        args = raw.split()
        provider = resolve_provider(args[0]) if args else None
        if provider is None:
            await update.message.reply_text(
                "Pakai: `/keys auto <provider>`\n\n"
                f"Provider: {', '.join(PROVIDERS)}\n\n"
                "Belum login di situsnya? `/login <url>` dulu.",
                parse_mode="Markdown",
            )
            return

        if self.harvester is None:
            await update.message.reply_text("Browser belum siap di server ini.")
            return

        host = host_from_url(provider.console_url)
        if self.profiles is not None and not self.profiles.permissions.is_allowed(host):
            await update.message.reply_text(
                f"Aku belum pernah diizinkan buka *{host}*.\n\n"
                f"Jalankan dulu: `/login {provider.console_url}`\n"
                "Setelah itu aku ingat izinnya.",
                parse_mode="Markdown",
            )
            return

        user_id = update.effective_user.id
        status = await update.message.reply_text(
            f"🤖 Mencoba ambil key *{provider.label}* sendiri...\n"
            "Aku buka konsolnya, cari tombol buat key, lalu baca hasilnya.",
            parse_mode="Markdown",
        )

        async def report(pct: int, text: str, screenshot=None) -> None:
            try:
                await status.edit_text(f"🤖 {pct}% — {text}")
            except Exception:  # noqa: BLE001
                pass

        result = await self.harvester.harvest(provider, user_id=user_id, progress=report)

        if result.needs_login:
            from app.llm.capacity import acquire_message

            await update.message.reply_text(
                acquire_message(provider.name, provider.console_url),
                parse_mode="Markdown",
            )
            return

        if not result.ok:
            for shot in result.screenshots[-1:]:
                try:
                    with open(shot, "rb") as handle:
                        await update.message.reply_photo(handle)
                except Exception:  # noqa: BLE001
                    pass
            await update.message.reply_text(
                f"❌ {result.message}\n\n"
                f"Alternatif cepat: buka {provider.console_url}, "
                f"bikin key, kirim ke sini: `/keys add {provider.name} <key>`",
                parse_mode="Markdown",
            )
            return

        # Verify before trusting, then store as the next free slot.
        probe = await self._probe_key(provider, result.key)
        if not probe["ok"]:
            await update.message.reply_text(
                f"Key terbaca tapi provider menolak: {probe['reason']}",
            )
            return

        from app.llm.credentials import append_key, next_slot

        env_path = Path(self.settings.env_file)
        slot = next_slot(env_path, provider.env_var)
        var = append_key(env_path, provider, result.key, slot)
        self._register_pool_key(provider, result.key, slot)

        await update.message.reply_text(
            f"✅ Key baru *{provider.label}* aktif ({result.masked_key()}) "
            f"sebagai `{var}`.\n\n"
            f"{probe['reason']}\n"
            "Langsung dipakai, tanpa restart. Cek: `/keys`",
            parse_mode="Markdown",
        )

    def _register_pool_key(self, provider, key: str, slot: int) -> None:
        """Make a freshly stored key usable without restarting the process."""
        pool = getattr(self.gateway, "key_pool", None)
        if pool is None:
            return
        from app.llm.key_pool import KeyState

        states = pool.states.setdefault(provider.name, [])
        if not any(s.key == key for s in states):
            states.append(KeyState(provider=provider.name, slot=slot, key=key))

    async def cmd_keys(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = list(context.args or [])
        sub = args[0].lower() if args else "status"

        if sub == "login":
            from app.llm.credentials import PROVIDERS, login_help, resolve_provider

            provider = resolve_provider(args[1]) if len(args) > 1 else None
            if len(args) > 1 and provider is None:
                await update.message.reply_text(
                    f"Provider `{' '.join(args[1:])}` tidak dikenal. "
                    f"Pilihan: {', '.join(PROVIDERS)}",
                    parse_mode="Markdown",
                )
                return
            await update.message.reply_text(login_help(provider), parse_mode="Markdown")
            return

        if sub in ("add", "tambah"):
            await self._keys_add(update, " ".join(args[1:]))
            return

        if sub in ("auto", "ambil"):
            await self.cmd_keys_auto(update, " ".join(args[1:]))
            return

        if sub in ("sumber", "capacity"):
            await self._keys_sources(update)
            return

        pool = getattr(self.gateway, "key_pool", None)
        if pool is None:
            await update.message.reply_text("Key pool belum aktif.")
            return
        lines = ["🔑 *Key Pool*", ""]
        lines += pool.status_lines()
        lines += [
            "",
            "*Cara nambah key:*",
            "`/keys auto gemini` — aku ambil sendiri (butuh login dulu)",
            "`/keys add gemini AIza...` — kamu kirim manual",
            "`/login <url>` — login sekali, aku simpan sesinya",
            "`/keys sumber` — dari mana saja key bisa didapat",
            "`/keys login gemini` — link + cara dapat key",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _keys_sources(self, update: Update) -> None:
        """Show where more keys come from and how much each provider gives."""
        from app.llm.capacity import CAPACITY, total_daily_tokens

        lines = ["🔑 *Sumber key & kapasitas*", ""]
        for name, cap in CAPACITY.items():
            lines.append(
                f"*{name}* — ~{cap.rough_daily_tokens:,} token/hari, "
                f"sampai {cap.keys_per_account} key\n{cap.daily_note}"
            )
        lines += [
            "",
            f"Total kalau semua dipakai: *~{total_daily_tokens():,} token/hari*.",
            "",
            "Cara nambah tanpa capek:",
            "1. `/login <url konsol>` — sekali per situs",
            "2. `/keys auto <provider>` — aku ambil key-nya sendiri",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _keys_add(self, update: Update, raw: str) -> None:
        """Validate a pasted key, store it, and start using it immediately."""
        from app.llm.credentials import (
            append_key,
            find_key_in_text,
            key_shape_ok,
            mask,
            next_slot,
        )

        found = find_key_in_text(raw)
        if found is None:
            await update.message.reply_text(
                "Aku tidak menemukan key di pesan itu.\n\n"
                "Format: `/keys add gemini AIza...`\n"
                "Butuh link login? `/keys login gemini`",
                parse_mode="Markdown",
            )
            return
        provider, key = found

        if not key_shape_ok(provider, key):
            await update.message.reply_text(
                f"Bentuk key itu tidak seperti key *{provider.label}*.\n"
                f"Cek lagi, atau lihat `/keys login {provider.name}`.",
                parse_mode="Markdown",
            )
            return

        probe = await self._probe_key(provider, key)
        if not probe["ok"]:
            await update.message.reply_text(
                f"Key *{provider.label}* ditolak provider.\n\n"
                f"Alasan: {probe['reason']}\n\n"
                "Key tidak aku simpan. Coba ambil yang baru dari "
                f"{provider.console_url}",
                parse_mode="Markdown",
            )
            return

        env_path = Path(self.settings.env_file)
        slot = next_slot(env_path, provider.env_var)
        var = append_key(env_path, provider, key, slot)

        # Refresh the live pool so the key works now, not after a restart.
        pool = getattr(self.gateway, "key_pool", None)
        if pool is not None:
            from app.llm.key_pool import KeyState

            states = pool.states.setdefault(provider.name, [])
            if not any(s.key == key for s in states):
                states.append(KeyState(provider=provider.name, slot=slot, key=key))

        await update.message.reply_text(
            f"✅ Key *{provider.label}* tersimpan sebagai `{var}` ({mask(key)}).\n\n"
            f"{probe['reason']}\n"
            f"Sudah langsung dipakai — tidak perlu restart.\n\n"
            f"Cek: `/keys`",
            parse_mode="Markdown",
        )

    async def _probe_key(self, provider, key: str) -> dict:
        """Ask the provider whether the key works before trusting it."""
        import httpx

        checks = {
            "groq": ("https://api.groq.com/openai/v1/models", "Bearer"),
            "deepseek": ("https://api.deepseek.com/models", "Bearer"),
            "openrouter": ("https://openrouter.ai/api/v1/models", "Bearer"),
            "cerebras": ("https://api.cerebras.ai/v1/models", "Bearer"),
            "mistral": ("https://api.mistral.ai/v1/models", "Bearer"),
            "hf": ("https://huggingface.co/api/whoami-v2", "Bearer"),
            "gemini": (
                f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
                "none",
            ),
        }
        entry = checks.get(provider.name)
        if entry is None:
            return {"ok": True, "reason": "Provider tidak punya cek cepat; key disimpan."}
        url, auth = entry
        headers = {"Authorization": f"Bearer {key}"} if auth == "Bearer" else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - network trouble is not a bad key
            return {
                "ok": False,
                "reason": f"Tidak bisa menghubungi provider ({type(exc).__name__}). Coba lagi.",
            }
        if response.status_code == 200:
            return {"ok": True, "reason": "Provider menerima key ini."}
        if response.status_code in (401, 403):
            return {"ok": False, "reason": f"HTTP {response.status_code} — key invalid atau dicabut."}
        if response.status_code == 429:
            return {"ok": True, "reason": "Key valid, tapi kuota hariannya sedang habis."}
        return {"ok": False, "reason": f"HTTP {response.status_code} dari provider."}

    async def cmd_memori(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_confirmed(update, context, Intent.MEMORY_QUERY, {"mode": "stats"})

    async def cmd_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        body = " ".join(context.args) if context.args else ""
        await self._run_confirmed(update, context, Intent.FEEDBACK, {"text": f"feedback {body}"})

    async def cmd_batal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user:
            self.gate.cancel(user.id)
        await update.effective_message.reply_text("Oke, dibatalkan.")

    async def cmd_jadwal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or self.scheduler is None:
            return
        rows = await self.scheduler.list_for(user.id)
        if not rows:
            await update.effective_message.reply_text("Belum ada jadwal aktif.")
            return
        lines = ["*Jadwal aktif:*"]
        for row in rows:
            lines.append(f"- `{row['id']}` {row['run_at'][:16]} → {row['text'][:60]}")
        lines.append("\nBatalkan dengan /bataljadwal <id>")
        await send_text(update.effective_message, "\n".join(lines))

    async def cmd_bataljadwal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or self.scheduler is None or not context.args:
            await update.effective_message.reply_text("Pakai: /bataljadwal <id>")
            return
        try:
            schedule_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("ID harus angka.")
            return
        ok = await self.scheduler.cancel(user.id, schedule_id)
        await update.effective_message.reply_text("Dibatalkan." if ok else "ID itu tidak ada.")

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or not self._is_admin(user.id):
            await update.effective_message.reply_text("Khusus admin.")
            return
        usage = await self.store.usage_today(user.id)
        errors = await self.store.recent_errors(10)
        lines = [
            "*Admin panel*",
            f"Provider aktif: {', '.join(self.settings.available_providers()) or 'tidak ada'}",
            f"Model kecil: `{self.settings.small_model}`",
            f"Model besar: `{self.settings.big_model}`",
            f"Token hari ini: {usage['total_tokens']:,}",
            f"Cache hit: {usage['cached_hits']}",
            "",
            f"Error terakhir ({len(errors)}):",
        ]
        lines += [f"- [{e['module']}] {e['message'][:60]}" for e in errors[:5]] or ["- tidak ada"]
        await send_text(update.effective_message, "\n".join(lines))

    async def _run_confirmed(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, intent: Intent, args: dict
    ) -> None:
        route = Route(intent, args, confidence=1.0, source="command")
        if route.needs_confirm:
            await self._ask_confirmation(update.effective_message, update.effective_user.id, route)
            return
        await self._run_task(update, context, route, confirmed=True)
