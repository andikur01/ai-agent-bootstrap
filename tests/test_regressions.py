"""Regressions for the bugs reported from real Telegram usage.

Each test pins a failure that actually reached the user, so it cannot come back.
"""
from __future__ import annotations

import re

import pytest

from app.bot.handlers import MENU_SECTIONS, MENU_TEXT, menu_keyboard
from app.core.errors import InvalidApiKeyError, ProviderUnavailableError
from app.llm.gateway import _looks_like_auth_failure
from app.services.media_qa import check_video


# ------------------------------------------------- 1. menu must not eat "_"
def test_every_command_in_menu_is_backticked():
    """A bare "_" is Markdown italics, which hid "/buatgambar" from the user."""
    command_lines = [
        line
        for line in MENU_TEXT.splitlines()
        if line.strip().startswith("/") or re.match(r"^`?/[a-z_]+", line.strip())
    ]
    assert command_lines, "menu must list commands"
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
