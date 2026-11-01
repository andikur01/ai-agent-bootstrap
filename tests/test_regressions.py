"""Regressions for the bugs reported from real Telegram usage.

Each test pins a failure that actually reached the user, so it cannot come back.
"""
from __future__ import annotations

import os

import re

import pytest

from app.bot.handlers import MENU_SECTIONS, MENU_TEXT, menu_keyboard
from app.core.errors import InvalidApiKeyError, ProviderUnavailableError
from app.llm.gateway import _looks_like_auth_failure
from app.services.media_qa import check_video


# ------------------------------------------------- 1. menu must not eat "_"
def test_every_command_in_help_is_backticked():
    """A bare "_" is Markdown italics, which hid "/buatgambar" from the user."""
    from app.bot.handlers import HELP_TEXT as _HELP

    command_lines = [
        line
        for line in _HELP.splitlines()
        if line.strip().startswith("/") or re.match(r"^`?/[a-z_]+", line.strip())
    ]
    assert command_lines, "help must list commands"
    for line in command_lines:
        if "/" in line:
            # the command token itself must sit inside backticks
            assert "`/" in line, f"command not in backticks: {line!r}"


def test_menu_never_shows_an_underscore_eating_command():
    """No rendered line may contain a bare underscore inside a command name."""
    for line in MENU_TEXT.splitlines():
        stripped = line.strip()
        if not stripped.startswith("/"):
            continue
        token = stripped.split()[0]
        assert "_" not in token, f"underscore would be stripped by Markdown: {token!r}"


def test_menu_buttons_match_sections():
    keyboard = menu_keyboard()
    shown = [b.text for row in keyboard.inline_keyboard for b in row]
    for label, _ in MENU_SECTIONS.values():
        assert label in shown


# ------------------------------------------------- 2. empty prompt guard
@pytest.mark.asyncio
async def test_image_create_without_a_prompt_does_not_generate(settings, store, gateway, memory):
    """"/buat_gambar" with no text once produced a random image."""
    from app.core.intent import Intent, Route
    from app.core.orchestrator import Orchestrator
    from app.llm.budget import BudgetGuard

    orch = Orchestrator(
        settings=settings,
        store=store,
        gateway=gateway,
        memory=memory,
        cache=None,
        budget=BudgetGuard(store, settings.daily_token_budget),
    )

    called = False
    real_generate = orch.images.generate

    async def spy(*a, **k):
        nonlocal called
        called = True
        return await real_generate(*a, **k)

    orch.images.generate = spy
    outcome = await orch._do_image_create(  # noqa: SLF001 - unit under test
        1, Route(Intent.IMAGE_CREATE, {"text": "/buat_gambar"})
    )
    assert not called, "generator must not run without a prompt"
    assert "gambar apa" in outcome.text.lower()


# ------------------------------------------------- 3. QA must fail closed
def test_video_qa_rejects_when_it_cannot_probe(tmp_path, monkeypatch):
    """A file QA cannot inspect is unverified, so it must not pass."""
    import app.services.ffmpeg as ffmpeg_mod

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * 40_000)
    monkeypatch.setattr(
        ffmpeg_mod, "_resolved", ffmpeg_mod.FFmpegPaths("", "", "missing"), raising=False
    )
    verdict = check_video(video)
    assert not verdict.ok
    assert "diverifikasi" in verdict.summary()


# ------------------------------------------------- 4. honest provider errors
def test_invalid_key_is_not_reported_as_a_network_problem():
    err = Exception('GroqException - {"error":{"message":"Invalid API Key"}}')
    assert _looks_like_auth_failure(err)


def test_rate_limit_is_still_a_provider_problem():
    assert not _looks_like_auth_failure(Exception("429 Too Many Requests"))


def test_auth_error_and_unavailable_error_are_distinct():
    assert InvalidApiKeyError is not ProviderUnavailableError
    assert "ditolak" in InvalidApiKeyError.user_message


# ------------------------------------------------- 5. unknown command
@pytest.mark.asyncio
async def test_unknown_slash_command_does_not_fall_through_to_chat(settings):
    """A typo'd command used to burn an LLM call and return a provider error."""
    from app.core.intent import route_message

    route = await route_message("/buatgambar", None)
    assert route.source == "fallback"
    assert route.intent.value == "chat"


# ------------------------------------------------- 6. browser agent
def test_action_parser_reads_plain_json():
    from app.services.browser_agent import parse_action

    action = parse_action('{"action":"click","index":3,"reason":"buka"}')
    assert action.kind == "click"
    assert action.index == 3


def test_action_parser_survives_fenced_and_chatty_replies():
    """Models wrap JSON in prose and fences; a miss here stalls the whole run."""
    from app.services.browser_agent import parse_action

    fenced = parse_action('```json\n{"action":"type","index":2,"text":"halo"}\n```')
    assert fenced.kind == "type"
    assert fenced.text == "halo"

    chatty = parse_action('Oke, aku klik tombolnya.\n{"action":"click","index":5}')
    assert chatty.kind == "click"
    assert chatty.index == 5


def test_action_parser_turns_junk_into_done_not_a_crash():
    from app.services.browser_agent import parse_action

    assert parse_action("tidak bisa dilanjutkan").kind == "done"
    assert parse_action({"action": "terbang", "index": "x"}).kind == "done"


@pytest.mark.asyncio
async def test_browser_agent_types_clicks_and_reads_the_result(tmp_path, settings):
    """End-to-end on a real page: no mocks of the browser, only of the model."""
    from app.services.browser_agent import BrowserAgent

    page = tmp_path / "page.html"
    page.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Toko</title></head><body>"
        "<input name='q' placeholder='Cari produk'>"
        "<button onclick=\"document.getElementById('o').textContent='Hasil: sepatu lari'\">Cari</button>"
        "<p id='o'>Belum ada</p></body></html>",
        encoding="utf-8",
    )

    class ScriptedGateway:
        def __init__(self, script):
            self.script = list(script)
            self.prompts = []

        async def complete(self, messages, **kwargs):
            import json
            from types import SimpleNamespace

            self.prompts.append(messages[-1]["content"])
            step = self.script.pop(0) if self.script else {"action": "done", "answer": "selesai"}
            return SimpleNamespace(text=json.dumps(step))

    gateway = ScriptedGateway(
        [
            {"action": "type", "index": 1, "text": "sepatu lari", "reason": "isi kolom"},
            {"action": "click", "index": 2, "reason": "tekan Cari"},
            {"action": "done", "answer": "Hasil: sepatu lari", "reason": "selesai"},
        ]
    )
    agent = BrowserAgent(settings, gateway=gateway, outputs_dir=tmp_path / "outputs")
    run = await agent.run(f"file://{page}", "cari sepatu lari lalu lapor hasilnya")

    assert run.ok, run.error
    assert "sepatu lari" in run.answer
    assert len(run.steps) == 3
    # The model must have been shown the real input, not a guess.
    assert "Cari produk" in gateway.prompts[0]


@pytest.mark.asyncio
async def test_browser_agent_stops_at_the_step_budget(tmp_path, settings):
    """An agent that never says 'done' must not loop forever."""
    from app.services.browser_agent import BrowserAgent

    page = tmp_path / "p.html"
    page.write_text("<html><body><button>Tekan</button></body></html>", encoding="utf-8")

    class LoopingGateway:
        async def complete(self, messages, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(text='{"action":"scroll","reason":"terus"}')

    agent = BrowserAgent(
        settings, gateway=LoopingGateway(), outputs_dir=tmp_path / "outputs", max_steps=3
    )
    run = await agent.run(f"file://{page}", "jangan pernah selesai")
    assert not run.ok
    assert "Batas langkah" in run.answer
    assert len(run.steps) == 3


# ------------------------------------------------- 7. browse intent routing
@pytest.mark.asyncio
async def test_browse_request_routes_to_browse_intent(settings):
    from app.core.intent import Intent, route_message

    route = await route_message("buka https://contoh.com lalu cari harga tiket", None)
    assert route.intent is Intent.BROWSE


@pytest.mark.asyncio
async def test_browse_without_a_url_asks_for_one(settings, store, gateway, memory):
    from app.core.intent import Intent, Route
    from app.core.orchestrator import Orchestrator
    from app.llm.budget import BudgetGuard

    orch = Orchestrator(
        settings=settings,
        store=store,
        gateway=gateway,
        memory=memory,
        cache=None,
        budget=BudgetGuard(store, settings.daily_token_budget),
    )
    outcome = await orch._do_browse(  # noqa: SLF001 - unit under test
        1, Route(Intent.BROWSE, {"text": "buka situs itu"})
    )
    assert "url" in outcome.text.lower()


# ---------------------------------------------------------------------------
# Key pool: several keys per provider, rotated when one runs dry.
# ---------------------------------------------------------------------------

def _settings_with_pool(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app.core.config import Settings

    return Settings(_env_file=None)


def test_pool_reads_numbered_env_vars(monkeypatch):
    settings = _settings_with_pool(
        monkeypatch,
        GEMINI_API_KEY="key-one-1111",
        GEMINI_API_KEY_2="key-two-2222",
        GEMINI_API_KEY_3="key-three-3333",
    )
    pool = settings.provider_key_pool()
    assert len(pool["gemini"]) == 3
    assert pool["gemini"][0] == "key-one-1111"


def test_pool_does_not_duplicate_plain_and_numbered(monkeypatch):
    """GEMINI_API_KEY and GEMINI_API_KEY_1 mean the same slot."""
    settings = _settings_with_pool(
        monkeypatch, GEMINI_API_KEY="same-key-9999", GEMINI_API_KEY_1="same-key-9999"
    )
    assert settings.provider_key_pool()["gemini"] == ["same-key-9999"]


def test_key_pool_rotates_and_masks(monkeypatch):
    from app.llm.key_pool import build_key_pool

    settings = _settings_with_pool(
        monkeypatch, GEMINI_API_KEY="aaaa-1111", GEMINI_API_KEY_2="bbbb-2222"
    )
    pool = build_key_pool(settings, state_file=None)
    assert pool.active("gemini").mask() == "****1111"
    nxt = pool.rotate("gemini", Exception("429 rate limit"))
    assert nxt.mask() == "****2222"


def test_key_pool_never_returns_a_full_key_in_status(monkeypatch):
    from app.llm.key_pool import build_key_pool

    settings = _settings_with_pool(monkeypatch, GEMINI_API_KEY="supersecretkey-abcd")
    pool = build_key_pool(settings, state_file=None)
    for line in pool.status_lines():
        assert "supersecretkey-abcd" not in line
        assert "abcd" in line


def test_numbered_keys_are_redacted(monkeypatch):
    """A key only reachable via GEMINI_API_KEY_2 must still be scrubbed."""
    settings = _settings_with_pool(monkeypatch, GEMINI_API_KEY_2="hidden-key-7777")
    assert "hidden-key-7777" in settings.secret_values()


def test_rate_limit_detection_separates_throttle_from_bad_key():
    from app.llm.key_pool import looks_like_rate_limit

    assert looks_like_rate_limit(Exception("Error 429: rate limit exceeded"))
    assert looks_like_rate_limit(Exception("Resource exhausted: quota"))
    assert not looks_like_rate_limit(Exception("Invalid API key provided"))


# ---------------------------------------------------------------------------
# Adding keys at runtime (/keys add) - parse, validate, store, use.
# ---------------------------------------------------------------------------

def test_provider_alias_resolution():
    from app.llm.credentials import resolve_provider

    assert resolve_provider("google").name == "gemini"
    assert resolve_provider("google ai").name == "gemini"
    assert resolve_provider("deep seek").name == "deepseek"
    assert resolve_provider("huggingface").name == "hf"
    assert resolve_provider("ngawur") is None


def test_provider_guessed_from_key_shape():
    from app.llm.credentials import guess_provider_from_key

    assert guess_provider_from_key("AIza" + "x" * 32).name == "gemini"
    assert guess_provider_from_key("gsk_" + "a" * 40).name == "groq"
    assert guess_provider_from_key("hf_" + "b" * 30).name == "hf"
    assert guess_provider_from_key("ngawur") is None


def test_key_extracted_from_chatty_message():
    """Users paste keys with prose around them; find it anyway."""
    from app.llm.credentials import find_key_in_text

    found = find_key_in_text("nih key gw gsk_" + "a" * 40 + " buat groq")
    assert found is not None
    assert found[0].name == "groq"
    assert found[1] == "gsk_" + "a" * 40

    assert find_key_in_text("halo apa kabar") is None


def test_wrong_shaped_key_is_rejected():
    from app.llm.credentials import PROVIDERS, key_shape_ok

    gemini = PROVIDERS["gemini"]
    assert key_shape_ok(gemini, "AIza" + "z" * 32)
    assert not key_shape_ok(gemini, "asdfghjkl")


def test_new_key_gets_next_free_slot_and_is_used_immediately(tmp_path, monkeypatch):
    from app.llm.credentials import PROVIDERS, append_key, next_slot

    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=AIza" + "1" * 32 + "\n")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "1" * 32)

    assert next_slot(env, "GEMINI_API_KEY") == 2
    var = append_key(env, PROVIDERS["gemini"], "AIza" + "2" * 32, 2)
    assert var == "GEMINI_API_KEY_2"
    assert "GEMINI_API_KEY_2=AIza" + "2" * 32 in env.read_text()
    # Usable without a restart.
    assert os.environ["GEMINI_API_KEY_2"] == "AIza" + "2" * 32
    assert next_slot(env, "GEMINI_API_KEY") == 3


def test_login_help_always_points_at_a_real_url():
    from app.llm.credentials import PROVIDERS, login_help

    for provider in PROVIDERS.values():
        text = login_help(provider)
        assert "https://" in text
        assert "/keys add" in text
        assert "Login" in text or "login" in text


def test_new_providers_have_models_and_env_names():
    from app.llm.gateway import TIER_MODELS

    for provider in ("deepseek", "cerebras", "mistral"):
        assert provider in TIER_MODELS["small"]
        assert provider in TIER_MODELS["big"]


def test_new_provider_joins_order_even_if_not_in_fallback_list(monkeypatch):
    """A key added for a provider missing from LLM_FALLBACK_ORDER still works.

    Otherwise /keys add deepseek would report success and change nothing.
    """
    from app.core.config import Settings

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "d" * 40)
    monkeypatch.setenv("LLM_FALLBACK_ORDER", "groq,gemini")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "g" * 40)
    settings = Settings(_env_file=None)

    order = settings.available_providers()
    assert "deepseek" in order
    assert order.index("groq") < order.index("deepseek")


# ---------------------------------------------------------------------------
# Provider call path: logging, model names, reasoning budget.
# ---------------------------------------------------------------------------

def test_logging_extra_never_uses_reserved_logrecord_keys():
    """extra={"module": ...} raises KeyError on Python 3.13.

    That KeyError happened inside the success branch of complete(), so a good
    call was reported as a failed provider and the user got an error.
    """
    import re
    from pathlib import Path

    reserved = {"module", "name", "msg", "args", "levelname", "message"}
    offenders = []
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"extra=\{([^}]*)\}", text, re.DOTALL):
            for key in re.findall(r'"([a-z_]+)"\s*:', match.group(1)):
                if key in reserved:
                    offenders.append(f"{path}:{key}")
    assert not offenders, f"reserved LogRecord keys in extra=: {offenders}"


def test_redaction_filter_survives_reserved_extra():
    """The formatter must still work when a record carries a component field."""
    import logging

    from app.core.logging_setup import JsonFormatter, RedactionFilter

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "llm ok", None, None)
    record.component = "llm"
    record.user_id = 7
    RedactionFilter().filter(record)
    payload = JsonFormatter().format(record)
    assert "llm ok" in payload
    assert "component" in payload


def test_groq_models_are_current():
    """Groq retired llama-3.1-8b-instant; a dead model name breaks every reply."""
    from app.llm.gateway import TIER_MODELS

    assert "llama-3.1-8b-instant" not in TIER_MODELS["small"]["groq"]
    assert "llama-3.3-70b-versatile" not in TIER_MODELS["big"]["groq"]


def test_reasoning_models_get_a_minimum_output_budget():
    """gpt-oss thinks before answering; a small cap yields empty text."""
    from app.llm.gateway import REASONING_MIN_OUTPUT, _needs_reasoning_floor

    assert _needs_reasoning_floor("groq/openai/gpt-oss-20b")
    assert _needs_reasoning_floor("deepseek/deepseek-reasoner")
    assert not _needs_reasoning_floor("groq/llama-3.1-8b-instant")
    assert REASONING_MIN_OUTPUT >= 500


def test_empty_completion_is_retried_not_returned(monkeypatch):
    """An empty reply must not be handed to the user as success."""
    import asyncio
    from types import SimpleNamespace

    from app.llm.gateway import LLMGateway

    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        # First call: reasoning burned the budget, no text. Second: real answer.
        content = "" if calls["n"] == 1 else "halo"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
        )

    class FakeSettings:
        llm_max_retries = 1
        llm_timeout_seconds = 5
        small_model = "groq/openai/gpt-oss-20b"
        big_model = "groq/openai/gpt-oss-20b"

        def available_providers(self):
            return ["groq"]

        def provider_key_pool(self):
            return {"groq": ["gsk_" + "x" * 40]}

        groq_api_key = "gsk_" + "x" * 40
        gemini_api_key = ""
        openrouter_api_key = ""
        hf_token = ""
        openai_api_key = ""
        anthropic_api_key = ""
        deepseek_api_key = ""
        cerebras_api_key = ""
        mistral_api_key = ""
        custom_llm_api_key = ""
        custom_llm_base_url = ""

    gw = LLMGateway(FakeSettings())
    monkeypatch.setattr(gw, "_import", lambda: fake_acompletion)
    result = asyncio.run(
        gw.complete([{"role": "user", "content": "halo"}], tier="small", max_tokens=10)
    )
    assert result.text == "halo"
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# Self-service key acquisition: profiles, permissions, harvesting, capacity.
# ---------------------------------------------------------------------------

def test_host_extracted_from_url():
    from app.services.browser_session import host_from_url

    assert host_from_url("https://console.groq.com/keys") == "console.groq.com"
    assert host_from_url("http://127.0.0.1:8899/keys") == "127.0.0.1"
    assert host_from_url("console.groq.com/keys") == "console.groq.com"


def test_site_permission_is_asked_once_then_remembered(tmp_path):
    """The user asked not to be re-prompted for a site already allowed."""
    from app.services.browser_session import SitePermissions

    perms = SitePermissions(tmp_path / "perms.json")
    assert not perms.is_allowed("console.groq.com")
    assert not perms.is_known("console.groq.com")

    perms.remember("console.groq.com", True)
    assert perms.is_allowed("console.groq.com")
    assert "console.groq.com" in perms.allowed_hosts()

    # Survives a restart.
    reloaded = SitePermissions(tmp_path / "perms.json")
    assert reloaded.is_allowed("console.groq.com")

    perms.remember("evil.example", False)
    assert not reloaded.__class__(tmp_path / "perms.json").is_allowed("evil.example")


def test_browser_profile_persists_and_can_be_wiped(tmp_path):
    from app.services.browser_session import BrowserProfiles

    profiles = BrowserProfiles(tmp_path / "browser_profiles")
    assert not profiles.has_profile(7)

    (profiles.profile_dir(7) / "Cookies").write_text("x")
    assert profiles.has_profile(7)
    assert profiles.wipe(7)
    assert not profiles.has_profile(7)


def test_create_key_button_matches_across_provider_wording():
    from app.services.key_harvester import CREATE_PATTERNS
    import re

    samples = [
        "Create new secret key",
        "Create API Key",
        "Create new key",
        "Generate new token",
    ]
    for text in samples:
        assert any(re.search(p, text, re.I) for p in CREATE_PATTERNS), text


def test_login_wall_detection_patterns():
    from app.services.key_harvester import LOGIN_WALL_PATTERNS
    import re

    for text in ("Sign in", "Log in", "Continue with Google", "Masuk"):
        assert any(re.search(p, text, re.I) for p in LOGIN_WALL_PATTERNS), text


def test_key_regex_extracts_each_provider_shape():
    """The scraper must find the key in whatever element the page uses."""
    import re

    from app.services.key_harvester import KEY_SHAPE

    cases = {
        "gemini": "AIza" + "a" * 35,
        "groq": "gsk_" + "b" * 40,
        "openrouter": "sk-or-v1-" + "c" * 40,
        "hf": "hf_" + "d" * 30,
    }
    for provider, key in cases.items():
        blob = f"Your new key: {key} (copy it now)"
        match = re.search(KEY_SHAPE[provider], blob)
        assert match and match.group(0) == key, provider


def test_exhausted_reports_only_fully_benched_providers():
    from app.llm.key_pool import KeyPool, KeyState

    pool = KeyPool(state_file=None, cooldown_seconds=60)
    pool.states["groq"] = [
        KeyState(provider="groq", slot=1, key="gsk_" + "a" * 40),
        KeyState(provider="groq", slot=2, key="gsk_" + "b" * 40),
    ]
    pool.states["gemini"] = [KeyState(provider="gemini", slot=1, key="AIza" + "c" * 35)]

    assert pool.exhausted() == []

    pool.states["gemini"][0].cooldown_until = __import__("time").time() + 300
    assert pool.exhausted() == ["gemini"]
    assert pool.soonest_ready("gemini") > 0
    assert pool.soonest_ready("groq") == 0


def test_capacity_numbers_are_plausible():
    from app.llm.capacity import CAPACITY, total_daily_tokens

    assert total_daily_tokens() > 1_000_000
    for name, cap in CAPACITY.items():
        assert cap.keys_per_account >= 1, name
        assert cap.rough_daily_tokens > 0, name
        assert cap.daily_note, name


def test_acquire_message_names_the_console_and_the_command():
    from app.llm.capacity import acquire_message

    text = acquire_message("gemini", "https://aistudio.google.com/apikey")
    assert "https://aistudio.google.com/apikey" in text
    assert "/keys add gemini" in text


# ---------------------------------------------------------------------------
# Live view: the user must be able to watch and click the agent's browser.
# ---------------------------------------------------------------------------

def test_live_view_url_points_at_vnc_html():
    from app.services.live_view import LiveView

    state = LiveView()
    url = state.url("https://work-2.example.dev")
    assert url.startswith("https://work-2.example.dev/")
    assert "vnc.html" in url
    # autoconnect so the user lands straight in the screen, not a form.
    assert "autoconnect=1" in url


def test_live_view_url_is_empty_without_a_host():
    """No host configured means no link, rather than a broken one."""
    from app.services.live_view import LiveViewService

    service = LiveViewService(settings=None)
    assert service.viewer_url() == ""


def test_live_view_reports_missing_tools_instead_of_crashing(monkeypatch):
    import asyncio

    from app.services.live_view import LiveViewService

    service = LiveViewService(settings=None)

    async def no_tools(name):
        return False

    monkeypatch.setattr(service, "_which", no_tools)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    state = asyncio.run(service.ensure())

    assert not state.running
    assert state.errors
    assert any("x11vnc" in e for e in state.errors)


def test_live_view_port_comes_from_settings():
    from dataclasses import dataclass

    from app.services.live_view import LiveViewService

    @dataclass
    class S:
        live_view_port: int = 12001
        live_view_host: str = "https://work-2.example.dev"

    service = LiveViewService(settings=S())
    assert service.state.web_port == 12001
    assert "work-2.example.dev" in service.viewer_url()


def test_live_view_password_is_not_guessable():
    from app.services.live_view import LiveViewService

    service = LiveViewService(settings=None)
    state = service.state
    assert state.password == ""
    # A generated password must be long enough to survive a VNC brute force.
    import secrets

    sample = secrets.token_urlsafe(9)
    assert len(sample) >= 12


# ---------------------------------------------------------------------------
# Narration vs command: the bot must not build a website because the user
# mentioned one. This was the user's loudest complaint.
# ---------------------------------------------------------------------------

NARRATION_CASES = [
    "saya pernah buat web tapi jangan sekarang",
    "aku tadi bikin gambar kucing lucu",
    "gue kemarin udah bikin sistem n8n",
    "aku pernah bikin video, kok error?",
]

COMMAND_CASES = [
    ("buatkan web portofolio", "web_static"),
    ("tolong buatkan website jualan", "web_static"),
    ("bikin web fullstack dengan login", "web_fullstack"),
    ("perbaiki website saya yang error", "web_repair"),
    ("buat gambar kucing", "image_create"),
    ("lanjutkan yang tadi", "continue"),
]

QUESTION_CASES = [
    "apakah webnya sudah jadi?",
    "web saya sudah berhasil belum?",
    "buat web apakah sudah jadi nih",
    "apakah gambarnya udah ada?",
    "apakah videonya sudah jadi?",
]


def test_narration_is_chat_not_a_command():
    """Mentioning past work must never start new work."""
    from app.core.intent import is_narration

    for text in NARRATION_CASES:
        assert is_narration(text), text


def test_imperative_still_wins_over_a_past_marker():
    """"aku sudah buat web, sekarang tambahkan login" is a command."""
    from app.core.intent import is_narration

    assert not is_narration("aku sudah buat web, sekarang tambahkan halaman login")
    assert not is_narration("tolong buatkan website jualan")


def test_question_about_work_is_detected():
    from app.core.intent import is_question_about_work

    for text in QUESTION_CASES:
        assert is_question_about_work(text), text
    assert not is_question_about_work("aku sudah buat web, sekarang tambahkan login")


def test_commands_still_route_correctly_without_a_gateway():
    """The guards must not swallow real orders."""
    import asyncio

    from app.core.intent import route_message

    async def run(text):
        return await route_message(text, gateway=None)

    for text, expected in COMMAND_CASES:
        route = asyncio.run(run(text))
        assert route.intent.value == expected, f"{text} -> {route.intent.value}"


def test_narration_routes_to_chat_without_a_gateway():
    import asyncio

    from app.core.intent import route_message

    async def run(text):
        return await route_message(text, gateway=None)

    for text in NARRATION_CASES:
        route = asyncio.run(run(text))
        assert route.intent.value == "chat", f"{text} -> {route.intent.value}"


def test_status_question_is_flagged_for_factual_answer():
    """ask_status makes the orchestrator answer from records, not the model."""
    import asyncio

    from app.core.intent import route_message

    async def run(text):
        return await route_message(text, gateway=None)

    route = asyncio.run(run("apakah webnya sudah jadi?"))
    assert route.args.get("ask_status") is True


def test_menu_explains_and_help_lists_the_commands():
    """The menu explains capabilities; every command stays reachable in /help."""
    from app.bot.handlers import HELP_TEXT, MENU_SECTIONS, MENU_TEXT

    assert set(MENU_SECTIONS) == {"web", "automation", "system"}
    # The menu describes what the agent does, in plain language.
    assert "Yang bisa aku kerjakan" in MENU_TEXT
    # Commands are documented, but behind /help rather than in the way.
    for command in ("/buat_web", "/buat_web_full", "/perbaiki_web"):
        assert command in HELP_TEXT
    # live view is automatic now, so it is not advertised as a command
    for command in ("/login", "/keys"):
        assert command in HELP_TEXT
    assert "live view menyala otomatis" in HELP_TEXT


def test_watchable_intents_use_enum_members():
    from app.bot.handlers import _WATCHABLE_INTENTS
    from app.core.intent import Intent

    assert Intent.WEB_STATIC in _WATCHABLE_INTENTS
    assert all(isinstance(i, Intent) for i in _WATCHABLE_INTENTS)


# ---------------------------------------------------------------------------
# Design system: generated sites must not be black-and-white slop.
# ---------------------------------------------------------------------------


def test_every_direction_has_a_real_palette():
    """Each direction must carry visible colour somewhere.

    Not necessarily in ``brand``: Neo is brutalist black on purpose, and its
    colour lives in the accent. What is forbidden is a palette with no colour at
    all, which is the grey site the user complained about.
    """
    from app.services.design_system import DIRECTIONS

    for key, direction in DIRECTIONS.items():
        assert len(direction.palette) >= 8, key
        chromatic = []
        for name, value in direction.palette.items():
            if not (value.startswith("#") and len(value) == 7):
                continue
            r, g, b = (int(value[i : i + 2], 16) for i in (1, 3, 5))
            if max(r, g, b) - min(r, g, b) > 40:
                chromatic.append(name)
        assert chromatic, f"{key} has no visible colour at all"
        assert direction.palette["brand"].startswith("#"), key


def test_stylesheet_bans_the_slop_colours():
    """#007bff is the bootstrap-blue giveaway of a template nobody designed."""
    from app.services.design_system import DIRECTIONS, base_stylesheet

    for direction in DIRECTIONS.values():
        css = base_stylesheet(direction)
        assert "#007bff" not in css
        assert "#6c757d" not in css
        # system-ui may appear as a fallback, but never as the only family.
        assert "font-family: system-ui" not in css
        assert "font-family: sans-serif" not in css
        assert "--brand:" in css
        assert "prefers-color-scheme" in css
        assert "prefers-reduced-motion" in css
        assert ":focus-visible" in css
        assert css.count("{") == css.count("}"), direction.key


def test_direction_routing_picks_something_sensible():
    from app.services.design_system import choose_direction

    assert choose_direction("toko laptop murah").key in {"aurora", "terracotta"}
    assert choose_direction("kedai kopi artisanal").key == "terracotta"
    assert choose_direction("portofolio fotografer").key == "atelier"
    assert choose_direction("aplikasi kripto trading").key == "midnight"
    # Unknown requests still get a real direction, never a blank.
    assert choose_direction("blah blah").key == "aurora"


def test_install_design_replaces_whatever_css_the_model_wrote():
    """The model must not be able to ship its own palette."""
    from app.services.design_system import choose_direction
    from app.services.web_builder import WebBuilder

    builder = WebBuilder.__new__(WebBuilder)
    spec = {
        "files": [
            {"path": "index.html", "content": "<html><head></head><body>hi</body></html>"},
            {"path": "styles.css", "content": "body{background:#007bff}"},
        ]
    }
    direction = choose_direction("toko laptop")
    out = builder._install_design(spec, direction, "toko laptop")
    css = next(f["content"] for f in out["files"] if f["path"] == "styles.css")
    assert "#007bff" not in css
    assert "--brand:" in css
    assert out["design"] == direction.key


def test_wire_head_guarantees_fonts_and_stylesheet():
    from app.services.design_system import choose_direction
    from app.services.web_builder import _wire_head

    direction = choose_direction("toko laptop")
    html = "<!DOCTYPE html><html><head><title>x</title></head><body>y</body></html>"
    out = _wire_head(html, direction)
    assert "fonts.googleapis.com" in out
    assert 'href="styles.css"' in out
    assert 'lang="id"' in out
    assert 'data-theme="light"' in out


def test_wire_head_does_not_duplicate_the_stylesheet():
    from app.services.design_system import choose_direction
    from app.services.web_builder import _wire_head

    direction = choose_direction("toko laptop")
    html = '<html lang="id" data-theme="light"><head><link rel="stylesheet" href="styles.css"></head><body>y</body></html>'
    out = _wire_head(html, direction)
    assert out.count('href="styles.css"') == 1


def test_token_estimate_is_a_range_not_a_false_precision():
    from app.core.orchestrator import TOKEN_ESTIMATES

    for intent, value in TOKEN_ESTIMATES.items():
        assert isinstance(value, tuple), intent
        low, high = value
        assert low < high, intent


# ---------------------------------------------------------------------------
# Live view: the screen the user asked for must actually come up.
# ---------------------------------------------------------------------------


def test_live_view_password_is_stable_across_restarts(tmp_path):
    """A password that changes on restart has to be re-copied every time."""
    from app.services.live_view import LiveViewService

    class FakeSettings:
        live_view_port = 12001
        live_view_host = "https://example.test"
        data_dir = str(tmp_path)

    first = LiveViewService(FakeSettings())._resolve_password()
    second = LiveViewService(FakeSettings())._resolve_password()
    assert first == second
    assert len(first) >= 8
    assert (tmp_path / "live_view_password").exists()


def test_live_view_password_honours_the_env_override(tmp_path, monkeypatch):
    from app.services.live_view import LiveViewService

    class FakeSettings:
        live_view_port = 12001
        live_view_host = "https://example.test"
        data_dir = str(tmp_path)

    monkeypatch.setenv("LIVE_VIEW_PASSWORD", "rahasia-pilihan-sendiri")
    assert LiveViewService(FakeSettings())._resolve_password() == "rahasia-pilihan-sendiri"


def test_live_view_password_file_is_not_world_readable(tmp_path):
    from app.services.live_view import LiveViewService

    class FakeSettings:
        live_view_port = 12001
        live_view_host = "https://example.test"
        data_dir = str(tmp_path)

    LiveViewService(FakeSettings())._resolve_password()
    mode = (tmp_path / "live_view_password").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_viewer_url_is_empty_without_a_configured_host():
    """Better no link than a link to localhost the phone cannot reach."""
    from app.services.live_view import LiveViewService

    class FakeSettings:
        live_view_port = 12001
        live_view_host = ""
        data_dir = "/tmp"

    assert LiveViewService(FakeSettings()).viewer_url() == ""


# ---------------------------------------------------------------------------
# "ambilkan key" must work from plain chat, not only from /keys auto.
# ---------------------------------------------------------------------------


def test_plain_language_key_request_routes_to_a_provider():
    from app.bot.handlers import _parse_harvest_request

    assert _parse_harvest_request("ambilkan key gemini") == "gemini"
    assert _parse_harvest_request("kasih aku api key groq dong") == "groq"
    assert _parse_harvest_request("tolong ambilin token openrouter") == "openrouter"
    assert _parse_harvest_request("Ambilkan API Key Gemini") == "gemini"


def test_plain_language_key_request_without_a_name_tries_all():
    from app.bot.handlers import _parse_harvest_request

    assert _parse_harvest_request("ambilkan semua key") == "all"
    assert _parse_harvest_request("ambil key dong") == "all"


def test_creating_an_account_is_not_a_key_request():
    """Account signup stays manual, so it must not reach the harvester."""
    from app.bot.handlers import _parse_harvest_request

    assert _parse_harvest_request("bikin akun gemini baru") is None
    assert _parse_harvest_request("daftar akun groq") is None
    assert _parse_harvest_request("register akun openrouter") is None


def test_ordinary_chat_is_not_mistaken_for_a_key_request():
    from app.bot.handlers import _parse_harvest_request

    assert _parse_harvest_request("buatkan web toko kopi") is None
    assert _parse_harvest_request("apa itu api key?") is None
    assert _parse_harvest_request("") is None
    assert _parse_harvest_request("lanjutkan yang tadi") is None


def test_auto_order_puts_the_generous_free_tiers_first():
    from app.bot.handlers import _provider_names_for_auto

    names = _provider_names_for_auto()
    assert names[0] == "gemini", "Gemini has the most generous free tier"
    assert "groq" in names


# ---------------------------------------------------------------------------
# The browser agent must be able to work behind a login, not just read public
# pages. Without the saved session it hits the wall and has nothing to sign in
# with, which made "buka akunku dan kerjakan ini" impossible.
# ---------------------------------------------------------------------------


def test_browser_agent_accepts_a_saved_session_and_display():
    import inspect

    from app.services.browser_agent import BrowserAgent

    params = inspect.signature(BrowserAgent.__init__).parameters
    assert "profiles" in params, "needs the profile dir to reuse the session"
    assert "vault" in params, "needs saved accounts to sign in on its own"
    assert "live_view" in params, "needs the display so the user can watch/take over"


def test_orchestrator_passes_them_through():
    import inspect

    from app.core.orchestrator import Orchestrator

    params = inspect.signature(Orchestrator.__init__).parameters
    assert "profiles" in params
    assert "vault" in params
    assert "live_view" in params


@pytest.mark.asyncio
async def test_browser_agent_signs_in_when_it_meets_a_login_wall(tmp_path):
    """A page behind a login must be entered with the saved account."""
    from app.services.account_vault import AccountVault
    from app.services.browser_agent import BrowserAgent

    vault = AccountVault(tmp_path / "accounts.json")
    vault.put("example.com", "aku@example.com", "rahasia")

    agent = BrowserAgent.__new__(BrowserAgent)
    agent.vault = vault
    agent.profiles = None
    agent.settings = None
    agent.outputs = tmp_path

    calls = {"signed": 0}

    class WallPage:
        url = "https://example.com/login"

        async def inner_text(self, _selector):
            return "Please log in to continue"

    # Replace the harvester helper with one that records the attempt.
    import app.services.key_harvester as kh

    original = kh.KeyHarvester.sign_in_with_saved_account

    async def fake_sign_in(self, page, host, *, progress=None):
        calls["signed"] += 1
        return True

    kh.KeyHarvester.sign_in_with_saved_account = fake_sign_in
    try:
        ok = await agent._maybe_sign_in(WallPage(), "https://example.com/login")
    finally:
        kh.KeyHarvester.sign_in_with_saved_account = original

    assert ok is True
    assert calls["signed"] == 1


@pytest.mark.asyncio
async def test_browser_agent_does_not_sign_in_without_a_saved_account(tmp_path):
    from app.services.account_vault import AccountVault
    from app.services.browser_agent import BrowserAgent

    vault = AccountVault(tmp_path / "accounts.json")  # empty
    agent = BrowserAgent.__new__(BrowserAgent)
    agent.vault = vault
    agent.profiles = None
    agent.settings = None
    agent.outputs = tmp_path

    class WallPage:
        url = "https://example.com/login"

        async def inner_text(self, _selector):
            return "Please log in"

    assert await agent._maybe_sign_in(WallPage(), "https://example.com/login") is False


def test_browser_agent_can_download_a_produced_file():
    """A tool site ends by handing over a file. Without a download action the
    agent reaches the final page and has nothing to send."""
    from app.services.browser_agent import _ACTION_SCHEMA, parse_action

    assert '"action":"download"' in _ACTION_SCHEMA
    action = parse_action('{"action":"download","index":7,"reason":"unduh hasil"}')
    assert action.kind == "download"
    assert action.index == 7


def test_a_downloaded_file_becomes_the_deliverable(tmp_path):
    """The file, not just a summary, must reach the user."""
    from app.core.orchestrator import Orchestrator

    produced = tmp_path / "hasil.mp4"
    produced.write_bytes(b"x" * 128)

    class FakeBrowser:
        downloaded = produced

    orch = Orchestrator.__new__(Orchestrator)
    orch.browser = FakeBrowser()

    files, note = orch._collect_download()
    assert files == [str(produced)]
    assert "hasil.mp4" in note
    assert orch.browser.downloaded is None, "must not be sent twice"

    # Nothing downloaded: no file, no note.
    orch.browser.downloaded = None
    assert orch._collect_download() == ([], "")


def test_a_missing_download_is_not_reported(tmp_path):
    from app.core.orchestrator import Orchestrator

    class FakeBrowser:
        downloaded = tmp_path / "sudah-dihapus.mp4"

    orch = Orchestrator.__new__(Orchestrator)
    orch.browser = FakeBrowser()
    assert orch._collect_download() == ([], "")


# ---------------------------------------------------------------------------
# "bikin video di flow" must go to the browser, not the local renderer. The
# local path would quietly produce a slideshow instead of using the site the
# user named - wrong output, no error, hard to notice.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("buatkan video di flow.google.com tentang kucing", "browse"),
        ("bikin video di runway", "browse"),
        ("bikin poster di canva", "browse"),
        ("buatkan gambar poster di canva.com", "browse"),
        ("buka akun aku di flow lalu buatkan video kucing", "browse"),
        ("buka google flow dan buatkan video", "browse"),
        ("buka https://flow.google.com dan bikin video kucing", "browse"),
        # A request with no named place still renders locally.
        ("buat video kucing", "video_create"),
        ("buatkan video penjelasan kopi", "video_create"),
        ("bikin animasi matematika", "video_create"),
    ],
)
def test_a_named_site_routes_to_the_browser(text, expected):
    from app.core.intent import classify_rules

    route = classify_rules(text)
    assert route is not None, f"no route for {text!r}"
    assert route.intent.value == expected, f"{text!r} -> {route.intent.value}"


def test_a_named_site_is_turned_into_a_url():
    """A bare site name has to become a URL the browser can open."""
    from app.core.intent import classify_rules

    route = classify_rules("bikin video di runway")
    assert route.intent.value == "browse"
    assert route.args["urls"], "must carry a URL for the browser"
    assert route.args["urls"][0].startswith("https://")


def test_a_named_site_routes_even_without_a_dot():
    from app.core.intent import KNOWN_SITES, classify_rules

    for name in ("flow", "sora", "canva", "suno"):
        route = classify_rules(f"bikin video di {name}")
        assert route is not None and route.intent.value == "browse", name
        assert name in KNOWN_SITES
