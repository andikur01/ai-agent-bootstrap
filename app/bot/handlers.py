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
import re
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
# Intents where a progress bar is honest: the user is waiting on real work.
# Chat, help and memory lookups answer in a second, so a bar there is theatre.
_PROGRESS_INTENTS = {
    Intent.IMAGE_CREATE, Intent.IMAGE_EDIT,
    Intent.VIDEO_CREATE, Intent.VIDEO_EDIT,
    Intent.AUDIO_CREATE, Intent.AUDIO_EDIT,
    Intent.TTS, Intent.STT,
    Intent.WEB_STATIC, Intent.WEB_FULLSTACK, Intent.WEB_REPAIR,
    Intent.N8N, Intent.LEARN, Intent.LINK_INGEST, Intent.THINK, Intent.BROWSE,
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
    "web": (
        "🌐 Web",
        "🌐 *WEB* - situs statis & fullstack\n\n"
        "`/buat_web` <deskripsi> - Situs statis profesional\n"
        "`/buat_web_full` <deskripsi> - Fullstack: login + database\n"
        "`/perbaiki_web` <link> - Perbaiki situs yang error\n\n"
        "Tulis saja: `buatkan web toko laptop`",
    ),
    "automation": (
        "⚙️ n8n",
        "⚙️ *N8N & JADWAL*\n\n"
        "`/buat_n8n` <deskripsi> - Workflow siap impor\n"
        "`/ingat` <waktu> <pesan> - Set reminder\n"
        "`/jadwal` - Lihat semua jadwal\n"
        "`/bataljadwal` <id> - Batalkan jadwal",
    ),
    "system": (
        "📊 System",
        "📊 *SYSTEM*\n\n"
        "`/status` - Status bot & provider\n"
        "`/status_model` - Model AI aktif\n"
        "`/memori` - Statistik memori\n"
        "`/help` - Bantuan lengkap",
    ),
}

# Full help stays available behind /help, but the menu shows only what is
# actually in use. A wall of commands for features nobody calls is noise.
HELP_EXTRA = (
    "⚡ *MODE*\n\n"
    "`/hidupkan` - Mode bebas: aku putuskan sendiri, tidak tanya tiap langkah\n"
    "`/hidupkan off` - Balik tanya dulu sebelum tugas berat\n"
    "`/mode` - Lihat mode aktif + token terpakai hari ini\n\n"
    "🔐 *AKUN (buat login otomatis di browser)*\n\n"
    "`/akun <situs> <email> <password>` - Simpan akun, aku yang login sendiri\n"
    "`/akun` - Lihat daftar akun (password disensor)\n"
    "`/akun hapus <situs>` - Hapus akun\n"
    "Kalau situs kirim kode verifikasi ke emailmu, aku minta kodenya ke kamu.\n\n"
    "🌐 *WEB*\n\n"
    "`/buat_web` <deskripsi> - Situs statis profesional\n"
    "`/buat_web_full` <deskripsi> - Fullstack: login + database\n"
    "`/perbaiki_web` <link> - Perbaiki situs yang error\n\n"
    "⚙️ *N8N & JADWAL*\n\n"
    "`/buat_n8n` <deskripsi> - Workflow siap impor\n"
    "`/ingat` <waktu> <pesan> - Set reminder\n"
    "`/jadwal` - Lihat semua jadwal\n"
    "`/bataljadwal` <id> - Batalkan jadwal\n\n"
    "🎨 *CREATIVE* (belum jadi fokus)\n\n"
    "`/buat_gambar` <deskripsi> - Generate gambar\n"
    "`/edit_gambar` <deskripsi> - Edit gambar terakhir\n"
    "`/buat_video` <deskripsi> - Buat video\n"
    "`/edit_video` <deskripsi> - Edit video terakhir\n"
    "`/text_to_voice` <teks> - Teks jadi suara\n"
    "`/voice_to_text` - Voice note jadi teks\n\n"
    "🔗 *LINK & MEDIA*\n\n"
    "`/baca_link` <url> - Rangkum isi link\n"
    "Kirim link langsung - YouTube, TikTok, IG, artikel\n"
    "Kirim voice note - Langsung ditranskrip\n\n"
    "🖥️ *BROWSER & KEY*\n\n"
    "(live view menyala otomatis di tugas panjang, tidak perlu perintah)\n"
    "`/login` <url> - Login sekali ke situs provider\n"
    "`/keys` - Status key pool\n"
    "`/keys auto` <provider> - Ambil key otomatis\n"
    "`/keys add` <provider> <key> - Kirim manual\n"
    "`/stok` - Stok token nyata (dihitung, bukan karangan)\n"
    "`/disk` - Cek penyimpanan, `/disk bersih` untuk bersihkan\n"
    "`/tes` - Aku tes kodeku sendiri dulu sebelum bilang selesai\n"
    "Ketik: `ambil key di situsbaru.com` - aku cari di situs mana pun\n"
    "Ketik: `simpan <key>` - baru aku masukkan ke .env\n\n"
    "💬 *CHAT*\n\n"
    "`/pikir` <topik> - Analisis 5 langkah\n"
    "`/belajar` <topik> - Cari, rangkum, simpan permanen\n"
    "`/feedback` <pesan> - Catat saranmu\n"
    "`/browse` <url> <tugas> - Kerjakan situs sendiri"
)

def _parse_harvest_request(text: str) -> str | None:
    """Return a provider name (or "all") when the user asks for a key.

    The browser exists so the user never has to open a provider console. Making
    them learn /keys auto first would waste that, so plain phrasing routes here:
    "ambilkan key gemini", "ambil semua key", "kasih aku api key groq".

    Returns None when this is not a key request, so normal routing continues.
    """
    if not text:
        return None
    lowered = text.lower()
    verb = re.search(
        r"\b(ambil|ambilkan|ambilin|kasih|kasihin|beri|berikan|dapatkan|tarik|pungut|isi|setup|pasang)\b",
        lowered,
    )
    noun = re.search(r"\b(api\s*key|apikey|key|token|kunci)\b", lowered)
    if not verb or not noun:
        return None
    # "bikin akun" is not this; account creation stays manual.
    if re.search(r"\b(akun|account|daftar|signup|sign\s*up|register)\b", lowered):
        return None

    from app.llm.credentials import PROVIDERS

    for name, provider in PROVIDERS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return name
        if provider.label and provider.label.lower() in lowered:
            return provider.label.lower().split()[0] if " " in provider.label else name

    # A site named in the message, for a provider nobody hard-coded: the user
    # points at "some new site that hands out keys" and expects the agent to go
    # there. Returning the URL is what makes that possible; without it the
    # request fell through to "provider tidak dikenal".
    site = re.search(r"https?://[^\s<>\"']+", text)
    if site:
        return f"site:{site.group(0).rstrip('.,;')}"
    domain = re.search(
        r"\b((?:[a-z0-9][a-z0-9-]*\.)+(?:com|net|org|io|ai|co|id|dev|app|me|xyz|tv|so|gg))\b",
        lowered,
    )
    if domain:
        return f"site:https://{domain.group(1)}"

    if re.search(r"\b(semua|all|everything|semuanya)\b", lowered):
        return "all"
    # Nothing named: take the autonomous route and try them all rather than
    # bouncing the question back at the user.
    return "all"


def _parse_save_request(text: str) -> list[str] | None:
    """Return the key values in a "simpan ..." message, or None.

    Discovery deliberately does not store anything: the user sees the values and
    approves. This is the approve step, and it only accepts strings that are
    actually key-shaped - so a message that happens to contain a password cannot
    be saved as a credential just because it was prefixed with "simpan".
    """
    if not text:
        return None
    if not re.match(r"^\s*/?(simpan|simpanin|pakai|pasang)\b", text, re.I):
        return None
    from app.services.key_discovery import looks_like_key

    values: list[str] = []
    for token in re.split(r"[\s,;]+", text):
        cleaned = token.strip().strip("`\"'")
        if looks_like_key(cleaned) and cleaned not in values:
            values.append(cleaned)
    return values or None


def _provider_names_for_auto() -> list[str]:
    from app.llm.credentials import PROVIDERS

    # Free tiers first, which is the order PROVIDERS is already written in.
    return list(PROVIDERS)


MENU_TEXT = (
    "🤖 *Aku asisten pribadimu - dan aku punya ahli di tiap bidang*\n\n"
    "Kamu tidak perlu hafal perintah. Tulis saja maumu pakai bahasa biasa,\n"
    "aku yang cari tahu maksudnya.\n\n"

    "*⚡ Menghidupkan bot (mode bebas)*\n"
    "`/hidupkan` - Aku berhenti nanya-nanya. Tugas berat langsung aku kerjakan,\n"
    "aku putuskan sendiri caranya, lalu aku lapor hasilnya.\n"
    "`/hidupkan off` - Balik tanya dulu sebelum tugas berat.\n"
    "`/mode` - Lihat mode aktif + token terpakai hari ini.\n\n"
    "Bedanya: tanpa mode bebas, tiap tugas berat aku minta izin dulu (ya/tidak)\n"
    "supaya kamu tahu biayanya. Dengan mode bebas, aku jalan sendiri. Pengaman\n"
    "biaya tetap jalan di dua-duanya, jadi token tidak kebablasan.\n\n"

    "*👥 Ahli di tiap bidang*\n"
    "Aku bukan satu asisten serba bisa. Tiap jenis kerja aku serahkan ke ahlinya,\n"
    "dan tiap ahli punya standar sendiri:\n"
    "🌐 Ahli Web - situs & web app. Dilarang lorem ipsum, wajib dark mode,\n"
    "   wajib lolos tes browser sebelum aku kirim ke kamu.\n"
    "🎨 Ahli Gambar - bikin & edit gambar, cek hasil dulu sebelum dikirim.\n"
    "🎬 Ahli Video - bikin & edit video, selalu cek durasi dan suara.\n"
    "🎵 Ahli Audio - suara & musik, teks ke suara dan suara ke teks.\n"
    "⚙️ Ahli Otomasi - workflow n8n yang benar-benar jalan.\n"
    "📚 Ahli Riset - cari di internet, rangkum, simpan permanen.\n"
    "🔗 Ahli Link - tonton/baca isi link lalu jawab pertanyaanmu.\n"
    "🧠 Ahli Analisis - berpikir sistematis untuk masalah rumit.\n"
    "💬 Asisten Ngobrol - ngobrol natural dan mengingat semuanya.\n\n"
    "Mau paksa satu ahli? Bilang: `buatkan video, kasih ke ahlinya`\n\n"

    "*🔐 Akses browser pakai akunmu*\n"
    "Kalau ada situs yang butuh login, aku bisa masuk sendiri pakai akun yang\n"
    "kamu simpan - jadi kamu tidak perlu buka browser lagi.\n"
    "Cara simpan: `/akun <situs> <email> <password>`\n"
    "  contoh: `/akun canva.com emailku@gmail.com rahasia123`\n"
    "Password disimpan terenkripsi di serverku, tidak pernah aku tulis ulang di\n"
    "chat atau di log, dan pesanmu berisi password aku hapus otomatis.\n"
    "`/akun` - lihat daftar akun tersimpan (password disensor).\n"
    "`/akun hapus <situs>` - hapus akun.\n\n"

    "*📧 Kalau situs kirim email verifikasi*\n"
    "Aku tidak bisa buka emailmu sendiri, jadi kalau situs minta kode, aku akan\n"
    "berhenti dan minta kode itu ke kamu di chat. Kamu kirim kodenya, aku\n"
    "lanjutkan sendiri dari titik itu - tidak perlu mulai dari awal.\n\n"

    "*🌐 Website*\n"
    "Situs statis (landing page, portofolio, toko) dan web fullstack dengan\n"
    "login + database. Aku tulis kodenya, aku tes sendiri pakai browser, aku\n"
    "screenshot, baru aku kirim. Kalau jelek aku perbaiki dulu.\n"
    "Contoh: `buatkan web toko kopi`\n\n"

    "*⚙️ Otomasi*\n"
    "Workflow n8n siap impor, plus pengingat dan jadwal berulang.\n"
    "Contoh: `buatkan workflow n8n untuk balas chat otomatis`\n\n"

    "*🖼️ Gambar, video, suara*\n"
    "Buat dan edit gambar, buat dan edit video, teks ke suara, suara ke teks.\n"
    "Contoh: `buatkan poster anak-anak`\n\n"

    "*🔗 Link*\n"
    "Kirim link apa saja - YouTube, TikTok, IG, artikel. Aku ambil isinya,\n"
    "aku tonton/transkrip, lalu jawab pertanyaanmu tentang isi link itu.\n"
    "Kalau linknya diblokir, aku coba cara lain dulu. Kalau tetap gagal, aku\n"
    "minta file-nya ke kamu.\n\n"

    "*💬 Ngobrol & ingatan*\n"
    "Aku ingat semua obrolan kita. \"lanjutkan yang tadi\", \"perbaiki yang\n"
    "kemarin\" - aku tahu persis yang mana. Tanya \"apakah webnya sudah jadi?\"\n"
    "kapan saja, aku jawab dari catatanku.\n\n"

    "*📚 Belajar*\n"
    "`belajar <topik>` - aku cari di internet, rangkum, simpan permanen.\n\n"

    "*💰 Token & biaya*\n"
    "Aku hemat: jawaban serupa aku ambil dari cache, konteks aku potong, model\n"
    "kecil untuk ngobrol, model besar hanya untuk tugas berat. Ada batas harian\n"
    "dan batas per tugas, jadi tidak ada lagi kejadian 1 juta token sehari.\n"
    "Tanya kapan saja: `berapa token terpakai?`\n\n"

    "Ketik `/help` untuk daftar perintah lengkap. Tapi ingat: kamu tidak butuh itu."
)

# /help shows the full command list; the menu stays short.
HELP_TEXT = HELP_TEXT + "\n\n" + HELP_EXTRA


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


def _is_token_request(text: str) -> bool:
    """Did the user actually ask what this cost?

    Printing the figure on every reply made the conversation unreadable, so it
    is shown only when asked for. The match is on the user's own words.
    """
    lowered = (text or "").lower()
    return any(
        phrase in lowered
        for phrase in ("berapa token", "token terpakai", "pakai token", "biaya token", "harga token")
    )


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
        vault=None,
        discovery=None,
    ):
        self.settings = settings
        self.store = store
        self.orch = orchestrator
        self.scheduler = scheduler
        self.profiles = profiles
        self.harvester = harvester
        self.live_view = live_view
        self.vault = vault or getattr(harvester, "vault", None)
        self.discovery = discovery
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

        # "ambilkan key gemini" is the whole point of the browser being here:
        # the user should not have to learn /keys auto. Detect the intent in
        # plain language and route straight to the harvester.
        harvest_target = _parse_harvest_request(text)
        if harvest_target is not None:
            await self.orch.memory.remember_message(user.id, "user", text, {})
            if harvest_target.startswith("site:"):
                await self._discover_on_site(update, harvest_target[5:])
            elif harvest_target == "all":
                await self._keys_auto_all(update)
            else:
                await self.cmd_keys_auto(update, harvest_target)
            return

        # Approval step for discovery: the agent found keys, the user says which
        # ones to keep. Kept separate from discovery on purpose - nothing reaches
        # .env without the user having seen the value first.
        save_values = _parse_save_request(text)
        if save_values:
            await self.orch.memory.remember_message(user.id, "user", text, {})
            await self._save_discovered_keys(update, save_values)
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
            # Free mode is the user's own switch ("hidupkan bot"): stop asking
            # permission for every step and start making the calls. The cost
            # guard still caps spending, so "act on your own" never means
            # "spend without limit".
            if await self._free_mode(user.id):
                route.args["confirmed"] = True
            else:
                await self._ask_confirmation(message, user.id, route)
                return

        await self._run_task(update, context, route)

    async def _free_mode(self, user_id: int) -> bool:
        try:
            prefs = await self.store.get_prefs(user_id)
        except Exception:  # noqa: BLE001 - never block a task on prefs
            return False
        return bool(prefs.get("free_mode"))

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
        # The user's own words for this turn, used below to decide whether they
        # actually asked about token usage.
        text = str(route.args.get("text") or "")

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

        # A progress bar belongs on work with real waiting in it. Showing one for
        # "hallo brp" put a progress bar and a "Mulai..." line above every
        # conversational reply, which is what made the chat feel like a machine.
        # Light intents just show the typing indicator.
        show_progress = route.intent in _PROGRESS_INTENTS
        reporter = ProgressReporter(
            lambda text: message.reply_text(text, parse_mode="Markdown"),
            title=f"{route.intent.value}",
            send_photo=lambda path, caption: message.reply_photo(
                Path(path).open("rb"), caption=redact(caption)[:1024]
            ),
        ) if show_progress else None

        async def progress(percent: int, note: str, *, screenshot: str = "") -> None:
            if reporter is not None:
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
            # The token figure used to be printed under every reply, and the
            # user found it noisy: "bro chat bisa kok di tampilan habis berapa
            # token sih". It is also an estimate, not a measurement, so putting
            # it on every message reads as precision that is not there. It now
            # lives behind /stok and /status, where someone asking for it will
            # look, and stays off the conversation.
            if outcome.tokens and _is_token_request(text):
                body += f"\n\n_Token terpakai: {outcome.tokens:,}_"
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
        # /start explains what the agent does. The command list lives in /help
        # for the rare case someone wants to type instead of talk.
        await send_text(update.effective_message, MENU_TEXT)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await send_text(update.effective_message, HELP_TEXT)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain what the agent can do. No buttons: this is not a chooser.

        The user was explicit - they do not want to pick from a menu, they want
        to say a sentence and have the agent work out the rest. A keyboard here
        invited the opposite behaviour, so the text stands alone.
        """
        await send_text(update.effective_message, MENU_TEXT)

    async def cmd_hidupkan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn the bot loose: decide on its own instead of asking each time.

        The user asked for this in these words - "cara agar menghidupkan bot",
        "kalau berhasil begini, izinkan dia masuk browser" - so the command is
        named after their phrasing. It flips one preference. The cost guard
        still applies, because acting freely and spending without limit are not
        the same thing.
        """
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        try:
            self._authorise(user.id)
        except UnauthorizedUserError as exc:
            await message.reply_text(exc.user_message)
            return

        arg = (context.args[0].lower() if context.args else "on")
        if arg in {"off", "mati", "stop", "nonaktif"}:
            await self.store.set_pref(user.id, "free_mode", False)
            await message.reply_text(
                "😴 Mode bebas dimatikan.\n"
                "Sekarang aku tanya dulu sebelum tugas berat."
            )
            return

        await self.store.set_pref(user.id, "free_mode", True)
        await message.reply_text(
            "⚡ *Mode bebas aktif.*\n\n"
            "Mulai sekarang aku tidak tanya-tanya lagi untuk tugas berat - aku "
            "putuskan sendiri, kerjakan, lalu lapor hasilnya.\n\n"
            "Aku tetap minta ke kamu hanya untuk hal yang cuma kamu yang bisa: "
            "login, izin, atau kode verifikasi yang dikirim ke emailmu.\n\n"
            "Pengaman biaya tetap jalan, jadi token tidak kebablasan.\n"
            "Matikan kapan saja: `/hidupkan off`",
            parse_mode="Markdown",
        )

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the current mode and the daily token situation."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        try:
            self._authorise(user.id)
        except UnauthorizedUserError as exc:
            await message.reply_text(exc.user_message)
            return

        free = await self._free_mode(user.id)
        lines = [
            "⚙️ *Mode sekarang*",
            f"Bebas bertindak: {'⚡ aktif' if free else '😴 mati'}",
        ]
        budget = getattr(self.orch, "budget", None)
        if budget is not None:
            lines.append("")
            lines.append(await budget.summary(user.id))
        lines.append("")
        lines.append("Ubah: `/hidupkan` atau `/hidupkan off`")
        await message.reply_text("\n".join(lines), parse_mode="Markdown")

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

    async def cmd_akun(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Store a login the agent may use, then delete the message holding it.

        The user asked for this explicitly and owns the accounts: a throwaway
        email, handed over once, so the agent can sign in and fetch keys without
        the user opening a console. The password is written to a 0600 file under
        the gitignored data directory, and the Telegram message carrying it is
        deleted so it does not stay in the chat history.
        """
        user_id = update.effective_user.id
        self._authorise(user_id)
        if self.vault is None:
            await update.message.reply_text("Penyimpanan akun belum aktif.")
            return

        args = list(context.args or [])
        if not args or args[0].lower() in ("list", "daftar"):
            saved = self.vault.describe()
            body = "\n".join(f"• `{line}`" for line in saved) if saved else "(belum ada)"
            await update.message.reply_text(
                "🔐 *Akun tersimpan*\n" + body + "\n\n"
                "Tambah: `/akun <situs> <email> <password>`\n"
                "Hapus: `/akun hapus <situs>`\n\n"
                "Password disimpan lokal (0600), tidak pernah masuk log atau git.",
                parse_mode="Markdown",
            )
            return

        if args[0].lower() in ("hapus", "delete", "forget"):
            if len(args) < 2:
                await update.message.reply_text("Pakai: `/akun hapus <situs>`", parse_mode="Markdown")
                return
            gone = self.vault.forget(args[1])
            await update.message.reply_text(
                "Sudah aku hapus." if gone else f"Tidak ada akun untuk `{args[1]}`.",
                parse_mode="Markdown",
            )
            return

        if len(args) < 3:
            await update.message.reply_text(
                "Pakai: `/akun <situs> <email> <password>`\n\n"
                "Contoh:\n`/akun google.com akunsaya@gmail.com rahasia123`\n\n"
                "Aku pakai untuk login sendiri saat ambil key. "
                "Pesannya aku hapus setelah tersimpan.",
                parse_mode="Markdown",
            )
            return

        host = args[0].lower().strip().strip("<>")
        email = args[1]
        password = " ".join(args[2:])

        # Delete first: the message holds a password, and a failure to store is
        # better than a password left sitting in the chat history.
        try:
            await update.message.delete()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not delete the password message: %s", exc)

        account = self.vault.put(host, email, password)
        await update.message.reply_text(
            f"🔐 Tersimpan untuk `{account.host}`: {account.email}\n"
            f"Password: `{account.masked_password()}`\n\n"
            "Sekarang aku bisa login sendiri ke situs itu. "
            "Kalau ada captcha, aku minta kamu selesaikan di layar live view.",
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

        url = args[0].strip().strip("<>").strip()
        # The placeholder in the help text got typed literally and became a real
        # navigation attempt to https://<url>. Say what is wrong instead of
        # letting Chromium produce a protocol error the user cannot act on.
        if not url or "<" in url or ">" in url or url.lower() in {"url", "link", "situs", "site"}:
            await update.message.reply_text(
                "Itu masih teks contoh, bukan link sungguhan.\n\n"
                "Ganti `<url>` dengan alamat situsnya, contoh:\n"
                "`/login https://aistudio.google.com/apikey`\n"
                "`/login https://console.groq.com/keys`\n\n"
                "Atau lebih gampang: `ambil key gemini` dan aku yang urus.",
                parse_mode="Markdown",
            )
            return
        if not url.startswith("http"):
            url = "https://" + url
        if not re.match(r"^https?://[a-z0-9.\-]+\.[a-z]{2,}(/|$|:)", url, re.I):
            await update.message.reply_text(
                f"`{url}` kelihatannya bukan alamat situs yang benar.\n\n"
                "Contoh yang benar: `/login https://aistudio.google.com/apikey`",
                parse_mode="Markdown",
            )
            return
        if self.harvester is None:
            await update.message.reply_text("Browser belum siap di server ini.")
            return

        from app.services.browser_session import host_from_url

        host = host_from_url(url)
        if self.profiles is not None:
            self.profiles.permissions.remember(host, True)

        seconds = int(getattr(self.settings, "browser_login_seconds", 240))
        view = await self._live_view_hint()
        status = await update.message.reply_text(
            f"🌐 Membuka browser di {host}...\n\n"
            f"{view}\n"
            f"Aku tunggu sampai {seconds} detik. Kalau sudah masuk, aku bilang.",
        )

        async def report(pct: int, text: str, screenshot=None) -> None:
            try:
                await status.edit_text(f"🌐 {pct}% — {text}")
            except Exception:  # noqa: BLE001 - edits can be rate-limited
                pass
            if screenshot:
                try:
                    with open(screenshot, "rb") as handle:
                        await update.message.reply_photo(handle, caption=text[:1000])
                except Exception:  # noqa: BLE001
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
            f"Kalau sudah: `ambil key {host.split('.')[0]}`",
            parse_mode="Markdown",
        )

    async def _live_view_hint(self) -> str:
        """Tell the user where to actually type, with the password if needed.

        'Login in that window' is useless advice on a headless host: there is no
        window on their screen. The live view URL is the window, so it has to be
        in the message - and it is the difference between the user being able to
        finish a login and the login silently never happening.
        """
        if self.live_view is None:
            return "Login lewat browser yang sudah aku buka."
        try:
            state = await self.live_view.ensure()
        except Exception as exc:  # noqa: BLE001
            log.debug("live view unavailable for login: %s", exc)
            return "Login lewat browser yang sudah aku buka."
        url = self.live_view.viewer_url() if state.running else ""
        if not url:
            return "Login lewat browser yang sudah aku buka."
        return (
            f"👉 *Buka layarnya di HP:*\n{url}\n"
            f"Password: `{state.password}`\n\n"
            "Di layar itu kamu lihat browsernya. Klik kolom email, ketik, "
            "lalu klik Next. Kamu yang isi, aku tidak pernah lihat passwordnya."
        )

    async def cmd_keys_auto(self, update: Update, raw: str) -> None:
        """Fetch a new key from a console the user is already signed into."""
        from app.llm.credentials import PROVIDERS, resolve_provider
        from app.services.browser_session import host_from_url

        args = raw.split()
        provider = resolve_provider(args[0]) if args else None
        if provider is None:
            if args and args[0].strip().strip("<>").lower() in {"provider", "<provider>"}:
                await update.message.reply_text(
                    "Itu masih teks contoh. Sebut providernya, atau aku coba "
                    "semuanya:\n\n"
                    f"Provider: {', '.join(PROVIDERS)}\n\n"
                    "Contoh: `ambil key gemini` atau `ambil semua key`",
                    parse_mode="Markdown",
                )
                return
            # No provider named: do all of them rather than asking again.
            await self._keys_auto_all(update)
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

    async def _save_discovered_keys(self, update: Update, values: list[str]) -> None:
        """Store keys the user approved, after checking the provider accepts them.

        Each value is probed before it is written. A key that a provider rejects
        is reported and skipped rather than stored - an unusable key in .env
        looks like capacity that does not exist, and the user would only find out
        when the bot fell over mid-task.
        """
        from app.llm.credentials import (
            append_key,
            guess_provider_from_key,
            next_slot,
        )

        message = update.effective_message
        saved: list[str] = []
        rejected: list[str] = []

        for value in values:
            provider = guess_provider_from_key(value)
            if provider is None:
                rejected.append(f"`{value[:8]}...` — tidak cocok provider mana pun")
                continue

            probe = await self._probe_key(provider, value)
            if not probe["ok"]:
                rejected.append(f"{provider.label}: {probe['reason']}")
                continue

            env_path = Path(self.settings.env_file)
            slot = next_slot(env_path, provider.env_var)
            var = append_key(env_path, provider, value, slot)
            self._register_pool_key(provider, value, slot)
            saved.append(f"{provider.label} → `{var}`")

        lines = []
        if saved:
            lines.append(f"✅ Tersimpan dan langsung aktif ({len(saved)}):")
            lines += [f"• {s}" for s in saved]
        if rejected:
            lines.append("")
            lines.append(f"⚠️ Tidak aku simpan ({len(rejected)}):")
            lines += [f"• {r}" for r in rejected]
        if saved:
            lines += ["", "Cek stok sekarang: `/stok`"]
        await message.reply_text("\n".join(lines) or "Tidak ada yang bisa disimpan.", parse_mode="Markdown")

    async def _discover_on_site(self, update: Update, url: str) -> None:
        """Go to a site the user names and report every key visible there.

        This is the answer to "ada web baru yang menyediakan api key banyak,
        bisa ga?" - the old code only knew seven consoles, so any site nobody
        had hard-coded was unreachable. Here the agent opens whatever it is
        given, signs in with a saved account if one matches, and reads the page.

        Nothing is stored automatically. The values come back to the user for
        approval, because a scraper that writes straight into .env is one bad
        match away from putting a password in a credential slot.
        """
        message = update.effective_message
        if self.discovery is None:
            await message.reply_text("Browser belum siap di server ini.")
            return

        from app.services.browser_session import host_from_url

        host = host_from_url(url)
        status = await message.reply_text(
            f"🔎 Aku buka *{host}* dan cari key di sana.\n"
            "Kalau ada akun tersimpan untuk situs itu, aku login sendiri dulu.",
            parse_mode="Markdown",
        )

        async def report(pct: int, text: str, screenshot=None) -> None:
            try:
                await status.edit_text(f"🔎 {pct}% — {text}")
            except Exception:  # noqa: BLE001
                pass
            if screenshot:
                try:
                    with open(screenshot, "rb") as handle:
                        await message.reply_photo(handle, caption=text[:900])
                except Exception:  # noqa: BLE001
                    pass

        try:
            result = await self.discovery.discover(
                url, user_id=update.effective_user.id, progress=report
            )
        except Exception as exc:  # noqa: BLE001 - report, do not crash the bot
            log.warning("key discovery failed on %s: %s", host, exc)
            await message.reply_text(f"Gagal memeriksa {host}: {str(exc)[:200]}")
            return

        # The screenshots are evidence of what the agent actually saw. They go
        # through the blurring presenter, so a key visible on screen is masked
        # in the image even though we send the value as text.
        if result.screenshots:
            from app.bot.presenter import send_screenshots

            await send_screenshots(message, [str(p) for p in result.screenshots[-2:]])

        if not result.keys:
            await message.reply_text(
                f"Tidak ada key yang aku temukan di *{host}*.\n\n"
                "Kemungkinan: belum login, halamannya butuh dibuat key dulu, "
                "atau key-nya tidak ditampilkan di halaman itu.\n\n"
                f"Kalau mau aku login dulu: `/login {url}`",
                parse_mode="Markdown",
            )
            return

        lines = [f"🔎 *{len(result.keys)} key ditemukan di {host}*", ""]
        for found in result.keys:
            lines.append(f"• `{found.value}` — {found.kind}")
        lines += [
            "",
            "Simpan yang mana? Balas:",
            f"`simpan {result.keys[0].kind} <key>`",
            "",
            "Atau aku simpan semua yang bentuknya cocok:",
            f"`simpan semua di {host}`",
        ]
        await message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_tes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Prove the bot can test its own work - live, on demand.

        The user wanted to see this behaviour rather than be told about it, so
        /tes runs a real script in the sandbox and reports what actually
        happened: a working one, a crashing one, and one that tries to read the
        bot's secrets. The last one matters most - it is the check that shows
        the sandbox is real, not a claim.
        """
        sandbox = getattr(self.orch, "sandbox", None)
        if sandbox is None:
            await update.effective_message.reply_text("Sandbox tidak aktif.")
            return

        message = await update.effective_message.reply_text(
            "🧪 Aku tes diriku sendiri, sebentar…"
        )

        lines = [f"🧪 *Hasil tes sendiri*\n\nIsolasi: {sandbox.describe()}"]
        strength = sandbox.isolation_strength()
        if strength == "kuat":
            lines.append("Kekuatan isolasi: *kuat* — kode tidak bisa baca `.env` bot.")
        else:
            lines.append(
                "Kekuatan isolasi: *lemah* — Docker tidak bisa diakses, jadi kode "
                "jalan sebagai subprocess. Environment sudah dibersihkan, tapi "
                "file di server masih bisa dibaca. Aku bilang ini terus terang."
            )

        with sandbox.workspace() as ws:
            # 1. Kode yang benar harus lulus.
            ws.write("benar.py", "print('aku jalan')\nprint(6 * 7)")
            good = await ws.run("benar.py")
            lines.append(
                f"\n1️⃣ Kode benar → {'✅ LULUS' if good.ok else '❌ GAGAL'}\n"
                f"`{good.stdout.strip().splitlines()[-1] if good.stdout.strip() else '-'}`"
            )

            # 2. Kode yang error harus terdeteksi gagal.
            ws.write("salah.py", "raise ValueError('ini sengaja error')")
            bad = await ws.run("salah.py")
            lines.append(
                f"\n2️⃣ Kode error → {'✅ terdeteksi gagal' if not bad.ok else '❌ SALAH: dianggap lulus'}"
                f"\n`exit {bad.exit_code}`"
            )

            # 3. Percobaan mencuri secret. Ini bukti isolasi sungguhan.
            ws.write(
                "curi.py",
                "import os\n"
                "p='/workspace/project/ai-agent-bootstrap/.env'\n"
                "print('BACA_ENV=' + str(os.path.exists(p)))\n"
                "print('TOKEN=' + str(os.environ.get('TELEGRAM_BOT_TOKEN')))",
            )
            spy = await ws.run("curi.py")
            leaked = "BACA_ENV=True" in spy.stdout or (
                "TOKEN=" in spy.stdout and "TOKEN=None" not in spy.stdout
            )
            lines.append(
                f"\n3️⃣ Coba curi `.env` → {'❌ BOCOR' if leaked else '✅ gagal (aman)'}\n"
                f"`{spy.stdout.strip().replace(chr(10), ' | ') or spy.stderr.strip()[:80]}`"
            )

        lines.append(
            "\nItu tes nyata, bukan klaim. Setiap tugas berat (website, kode, media) "
            "aku jalankan lewat pemeriksaan yang sama sebelum aku bilang selesai."
        )
        await message.edit_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_disk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show how full the outputs directory is, and clean it on request."""
        from app.services.retention import build_retention

        retention = build_retention(self.settings)
        text = context.args[0].lower() if context.args else ""

        if text in ("bersih", "clean", "bersihin"):
            report = retention.sweep(force=True)
            await update.effective_message.reply_text(
                f"{report.summary()}\n\n{retention.status()}", parse_mode="Markdown"
            )
            return

        await update.effective_message.reply_text(
            f"💾 *Penyimpanan*\n\n{retention.status()}\n\n"
            "Aku bersihkan otomatis tiap "
            f"{self.settings.retention_interval_hours} jam.\n"
            "Login tersimpan, memory, dan file terbaru *tidak pernah* aku hapus.\n\n"
            "Mau bersihkan sekarang? Ketik `/disk bersih`",
            parse_mode="Markdown",
        )

    async def cmd_stok(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report the real token stock, counted from stored keys.

        The user asked for a number and insisted it not be invented. So the
        count comes from the key pool and the environment, and the allowance is
        labelled an estimate - the provider never tells us the quota, so any
        precise figure would be a guess dressed up as a fact.
        """
        from app.llm.stock import build_stock_counter

        counter = build_stock_counter(self.settings, key_pool=getattr(self.gateway, "key_pool", None))
        stock = counter.count()

        status = await update.effective_message.reply_text(
            "📊 Aku hitung key yang tersimpan, lalu tanya langsung ke provider..."
        )
        try:
            await counter.verify(stock)
        except Exception as exc:  # noqa: BLE001 - a failed check is not a failed report
            log.debug("stock verification failed: %s", exc)
        await status.edit_text(stock.report(), parse_mode="Markdown")

    def _register_pool_key(self, provider, key: str, slot: int) -> None:
        """Make a freshly stored key usable without restarting the process."""
        pool = getattr(self.gateway, "key_pool", None)
        if pool is None:
            return
        from app.llm.key_pool import KeyState

        states = pool.states.setdefault(provider.name, [])
        if not any(s.key == key for s in states):
            states.append(KeyState(provider=provider.name, slot=slot, key=key))

    async def _keys_auto_all(self, update: Update) -> None:
        """Walk every provider and fetch whatever the saved sessions allow.

        This is the autonomous version of the key chore: the user says "ambil
        semua key" once and the agent works down the list, skipping providers it
        is not signed into yet and reporting exactly which login is still needed.
        One provider failing never stops the rest - the useful outcome is the
        keys it did get, not a clean sweep.
        """
        from app.llm.credentials import PROVIDERS

        message = update.effective_message
        names = list(PROVIDERS)
        status = await message.reply_text(
            f"🤖 Aku coba ambil key dari {len(names)} provider, satu per satu.\n"
            "Yang belum login akan aku lewati dan aku beri tahu.",
        )

        got: list[str] = []
        need_login: list[str] = []
        failed: list[str] = []

        for index, name in enumerate(names, start=1):
            provider = PROVIDERS[name]
            try:
                await status.edit_text(
                    f"🤖 {index}/{len(names)} — mencoba *{provider.label}*...",
                    parse_mode="Markdown",
                )
            except Exception:  # noqa: BLE001
                pass

            try:
                outcome = await self._harvest_one(provider, update)
            except Exception as exc:  # noqa: BLE001 - one provider must not stop the rest
                failed.append(f"{provider.label}: {str(exc)[:60]}")
                continue

            if outcome == "ok":
                got.append(provider.label)
            elif outcome == "login":
                need_login.append(provider.label)
            else:
                failed.append(provider.label)

        lines = [f"🤖 Selesai. Hasil dari {len(names)} provider:\n"]
        lines.append(f"✅ Dapat key baru: {', '.join(got) if got else 'belum ada'}")
        if need_login:
            lines.append(
                f"🔑 Perlu kamu login sekali: {', '.join(need_login)}\n"
                "   Jalankan `/login <url>` untuk yang kamu mau, lalu ulangi."
            )
        if failed:
            lines.append(f"❌ Gagal/lewat: {', '.join(failed)}")
        lines.append("\nCek semua key aktif: `/keys`")
        await message.reply_text("\n".join(lines))

    async def _harvest_one(self, provider, update: Update) -> str:
        """Harvest a single provider. Returns "ok", "login" or "failed"."""
        from app.llm.capacity import acquire_message

        if self.harvester is None:
            return "failed"

        user_id = update.effective_user.id
        result = await self.harvester.harvest(provider, user_id=user_id)
        if result.needs_login:
            return "login"
        if not result.ok:
            return "failed"

        probe = await self._probe_key(provider, result.key)
        if not probe["ok"]:
            return "failed"

        from app.llm.credentials import append_key, next_slot

        env_path = Path(self.settings.env_file)
        slot = next_slot(env_path, provider.env_var)
        append_key(env_path, provider, result.key, slot)
        self._register_pool_key(provider, result.key, slot)
        log.info("harvested %s key into slot %s", provider.name, slot)
        return "ok"

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
