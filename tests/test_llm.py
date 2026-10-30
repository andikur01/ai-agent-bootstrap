"""Token budgeting and gateway fallback tests."""
from __future__ import annotations

import pytest

from app.core.errors import BudgetExceededError, MissingSecretError, ProviderUnavailableError
from app.llm.budget import BudgetGuard
from app.llm.gateway import LLMGateway
from app.llm.tokens import count_message_tokens, count_tokens, trim_messages, trim_to_tokens


def test_count_tokens_is_positive_and_monotonic():
    assert count_tokens("") == 0
    short = count_tokens("halo dunia")
    longer = count_tokens("halo dunia " * 50)
    assert 0 < short < longer


def test_trim_to_tokens_respects_budget():
    text = "kata " * 5000
    trimmed = trim_to_tokens(text, 100)
    assert count_tokens(trimmed) <= 100
    assert trim_to_tokens(text, 100, keep_tail=True).endswith("kata ")


def test_trim_messages_keeps_system_and_last_turns():
    messages = [{"role": "system", "content": "SYSTEM PROMPT"}]
    for i in range(50):
        messages.append({"role": "user", "content": f"pesan {i} " + "kata " * 100})
        messages.append({"role": "assistant", "content": f"balasan {i} " + "kata " * 100})
    trimmed = trim_messages(messages, 800)
    assert count_message_tokens(trimmed) <= 800
    assert trimmed[0]["content"] == "SYSTEM PROMPT"
    assert trimmed[-1]["role"] == "assistant"


def test_trim_messages_trims_oversized_last_user_message():
    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "x" * 100000},
    ]
    trimmed = trim_messages(messages, 300)
    assert count_message_tokens(trimmed) <= 300


@pytest.mark.asyncio
async def test_budget_guard_blocks_over_budget(store):
    guard = BudgetGuard(store, daily_budget=1000)
    await guard.record(1, "m", 900, 50)
    with pytest.raises(BudgetExceededError):
        await guard.check(1, 200)
    await guard.check(1, 10)  # fits
    assert await guard.remaining(1) == 50


@pytest.mark.asyncio
async def test_budget_summary_renders(store):
    guard = BudgetGuard(store, daily_budget=5000)
    await guard.record(1, "groq/x", 100, 50, cached_hits=3)
    summary = await guard.summary(1)
    assert "Token hari ini" in summary and "groq/x" in summary


def test_gateway_without_keys_raises_missing_secret(settings):
    gw = LLMGateway(settings)
    assert not gw.available()
    with pytest.raises(MissingSecretError):
        gw.require_ready()


def test_gateway_candidate_order_follows_config(settings, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x" * 8)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "y" * 30)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "z" * 20)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    gw = LLMGateway(fresh)
    providers = [p for p, _ in gw.candidates("small")]
    assert providers[:3] == ["groq", "gemini", "openrouter"]
    assert all(m for _, m in gw.candidates("big"))
    config_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_gateway_falls_back_then_reports(settings, monkeypatch):
    """All providers fail -> ProviderUnavailableError listing every attempt."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 30)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()

    gw = LLMGateway(fresh)
    calls: list[str] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("429 rate limited")

    gw._litellm = fake_acompletion  # noqa: SLF001 - inject the fake at the seam
    with pytest.raises(ProviderUnavailableError) as exc:
        await gw.complete([{"role": "user", "content": "hi"}], tier="small")
    assert calls, "harus mencoba provider"
    assert "groq" in str(exc.value)
    config_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_gateway_records_usage_on_success(settings, monkeypatch, store):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "b" * 30)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    guard = BudgetGuard(store, fresh.daily_token_budget)
    gw = LLMGateway(fresh, budget=guard)

    class _Usage:
        prompt_tokens = 11
        completion_tokens = 7

    class _Msg:
        content = "halo dari model"

    class _Choice:
        message = _Msg()

    class _Resp:
        usage = _Usage()
        choices = [_Choice()]

    async def fake_acompletion(**kwargs):
        return _Resp()

    gw._litellm = fake_acompletion  # noqa: SLF001
    result = await gw.complete([{"role": "user", "content": "hi"}], tier="small", user_id=1)
    assert result.text == "halo dari model"
    usage = await guard.usage(1)
    assert usage["total_tokens"] == 18
    config_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_gateway_parses_json_with_code_fence(settings, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "c" * 30)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    gw = LLMGateway(fresh)

    async def fake(**kwargs):
        class M:
            content = '```json\n{"intent": "gambar", "confidence": 0.9}\n```'

        class C:
            message = M()

        class R:
            usage = None
            choices = [C()]

        return R()

    gw._litellm = fake  # noqa: SLF001
    parsed = await gw.complete_json([{"role": "user", "content": "x"}])
    assert parsed["intent"] == "gambar"
    config_module.get_settings.cache_clear()
