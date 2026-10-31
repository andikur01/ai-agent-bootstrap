"""Tests for the /menu surface: text, inline keyboard, and command wiring.

These drive the real handler methods with lightweight Telegram doubles, so a
broken command registration or a menu button that maps to the wrong intent
fails here rather than in the user's chat.
"""
from __future__ import annotations

import pytest

from app.bot.handlers import (
    MENU_PREFIX,
    MENU_SECTIONS,
    MENU_TEXT,
    TelegramBot,
    menu_keyboard,
    menu_section,
)
from app.core.intent import Intent


class FakeMessage:
    def __init__(self):
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self

    async def reply_photo(self, *args, **kwargs):
        return self


class FakeUser:
    id = 111


class FakeUpdate:
    def __init__(self):
        self.effective_message = FakeMessage()
        self.effective_user = FakeUser()
        self.callback_query = None


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = FakeUser()
        self.answers: list[str] = []
        self.edits: list[tuple[str, object]] = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class FakeCallbackUpdate:
    def __init__(self, data):
        self.callback_query = FakeQuery(data)
        self.effective_message = FakeMessage()
        self.effective_user = FakeUser()


class StubOrchestrator:
    def estimate_tokens(self, route):
        return 700

    def confirmation_prompt(self, route):
        return "Jalankan?"


@pytest.fixture
def bot(settings, store):
    return TelegramBot(settings=settings, store=store, orchestrator=StubOrchestrator(), scheduler=None)


def test_menu_text_lists_every_section_body():
    for _, body in MENU_SECTIONS.values():
        assert body in MENU_TEXT


def test_menu_keyboard_labels_cover_every_section():
    labels = [label for label, _ in MENU_SECTIONS.values()]
    keyboard = menu_keyboard()
    shown = [button.text for row in keyboard.inline_keyboard for button in row]
    for label in labels:
        assert label in shown


def test_menu_keyboard_buttons_carry_section_ids():
    keyboard = menu_keyboard()
    datas = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert f"{MENU_PREFIX}home" not in datas
    for section in MENU_SECTIONS:
        assert f"{MENU_PREFIX}{section}" in datas
    assert f"{MENU_PREFIX}help" in datas


def test_menu_section_home_returns_full_menu_and_has_back_button():
    body, keyboard = menu_section("home")
    assert body == MENU_TEXT
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert f"{MENU_PREFIX}home" in datas


def test_menu_section_unknown_falls_back_to_full_menu():
    body, _ = menu_section("tidak-ada")
    assert body == MENU_TEXT


def test_creative_section_mentions_the_commands_it_advertises():
    _, body = MENU_SECTIONS["creative"]
    for command in ("/buat_gambar", "/edit_gambar", "/buat_video", "/edit_video", "/text_to_voice"):
        assert command in body


@pytest.mark.asyncio
async def test_cmd_menu_sends_text_with_keyboard(bot):
    update = FakeUpdate()
    await bot.cmd_menu(update, FakeContext())
    assert update.effective_message.texts
    assert update.effective_message.markups[-1] is not None


@pytest.mark.asyncio
async def test_cmd_status_reports_providers_without_crashing(bot):
    update = FakeUpdate()
    await bot.cmd_status(update, FakeContext())
    joined = "\n".join(update.effective_message.texts)
    assert "Status bot" in joined
    assert "Provider LLM" in joined


@pytest.mark.asyncio
async def test_cmd_status_model_lists_tiers(bot):
    update = FakeUpdate()
    await bot.cmd_status_model(update, FakeContext())
    joined = "\n".join(update.effective_message.texts)
    assert "Model AI aktif" in joined
    assert "Tier small" in joined


@pytest.mark.asyncio
async def test_menu_callback_edits_message_in_place(bot):
    update = FakeCallbackUpdate(f"{MENU_PREFIX}creative")
    await bot.on_callback(update, FakeContext())
    assert update.callback_query.edits, "menu click must edit the message"
    text, markup = update.callback_query.edits[-1]
    assert "/buat_gambar" in text
    assert markup is not None


@pytest.mark.asyncio
async def test_menu_callback_back_to_home(bot):
    update = FakeCallbackUpdate(f"{MENU_PREFIX}home")
    await bot.on_callback(update, FakeContext())
    text, _ = update.callback_query.edits[-1]
    assert text == MENU_TEXT


@pytest.mark.parametrize(
    ("method", "intent"),
    [
        ("cmd_buat_gambar", Intent.IMAGE_CREATE),
        ("cmd_edit_gambar", Intent.IMAGE_EDIT),
        ("cmd_buat_video", Intent.VIDEO_CREATE),
        ("cmd_edit_video", Intent.VIDEO_EDIT),
        ("cmd_buat_web", Intent.WEB_STATIC),
        ("cmd_buat_web_full", Intent.WEB_FULLSTACK),
        ("cmd_perbaiki_web", Intent.WEB_REPAIR),
        ("cmd_buat_n8n", Intent.N8N),
        ("cmd_ingat", Intent.REMINDER),
        ("cmd_baca_link", Intent.LINK_INGEST),
        ("cmd_text_to_voice", Intent.TTS),
        ("cmd_voice_to_text", Intent.STT),
    ],
)
@pytest.mark.asyncio
async def test_alias_commands_route_to_the_right_intent(bot, monkeypatch, method, intent):
    captured: dict = {}

    async def fake_run_confirmed(update, context, route_intent, args):
        captured["intent"] = route_intent
        captured["args"] = args

    monkeypatch.setattr(bot, "_run_confirmed", fake_run_confirmed)
    await getattr(bot, method)(FakeUpdate(), FakeContext(["sesuatu"]))
    assert captured["intent"] is intent


@pytest.mark.asyncio
async def test_alias_command_passes_arguments_through(bot, monkeypatch):
    captured: dict = {}

    async def fake_run_confirmed(update, context, route_intent, args):
        captured.update(args)

    monkeypatch.setattr(bot, "_run_confirmed", fake_run_confirmed)
    await bot.cmd_buat_gambar(FakeUpdate(), FakeContext(["kucing", "astronot"]))
    assert captured["text"] == "kucing astronot"


# --------------------------------------------------------------------- guard
class GuardUpdate:
    def __init__(self, user_id, text=""):
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_message = FakeMessage()
        self.effective_message.text = text
        self.callback_query = None


@pytest.mark.asyncio
async def test_guard_blocks_stranger_commands(bot):
    from telegram.ext import ApplicationHandlerStop

    update = GuardUpdate(999, "/jadwal")
    with pytest.raises(ApplicationHandlerStop):
        await bot.on_guard(update, FakeContext())
    assert update.effective_message.texts, "stranger must be told why"


@pytest.mark.asyncio
async def test_guard_allows_allowlisted_user(bot):
    update = GuardUpdate(111, "/jadwal")
    await bot.on_guard(update, FakeContext())  # must not raise


@pytest.mark.asyncio
async def test_guard_lets_start_through_for_onboarding(bot):
    update = GuardUpdate(999, "/start")
    await bot.on_guard(update, FakeContext())  # must not raise


@pytest.mark.asyncio
async def test_guard_blocks_stranger_callback(bot):
    from telegram.ext import ApplicationHandlerStop

    update = GuardUpdate(999)
    update.effective_message = None
    update.callback_query = FakeQuery(f"{MENU_PREFIX}creative")
    with pytest.raises(ApplicationHandlerStop):
        await bot.on_guard(update, FakeContext())
    assert update.callback_query.answers == ["Tidak diizinkan."]


def test_application_registers_guard_and_all_menu_commands(settings, store, monkeypatch):
    """The real builder must wire every command the menu advertises."""
    from app.application import _build_telegram_app

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    bot = TelegramBot(settings=fresh, store=store, orchestrator=StubOrchestrator(), scheduler=None)
    app = _build_telegram_app(fresh, bot)
    registered = {
        command
        for group in app.handlers.values()
        for handler in group
        for command in (getattr(handler, "commands", None) or [])
    }
    for command in (
        "menu",
        "status",
        "status_model",
        "buat_gambar",
        "edit_gambar",
        "buat_video",
        "edit_video",
        "buat_web",
        "buat_web_full",
        "perbaiki_web",
        "buat_n8n",
        "ingat",
        "jadwal",
        "baca_link",
        "voice_to_text",
        "text_to_voice",
    ):
        assert command in registered, f"/{command} belum terdaftar"
    assert -1 in app.handlers, "guard harus di group -1"
