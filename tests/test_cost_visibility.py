"""The user must be able to see the cost ceiling before it is hit.

Their complaint was a million tokens in a day. The guard existed, but its
default was unlimited and nothing showed the limit, so the user found out from
the bill. These tests pin the default and the visible report.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings


def test_the_default_daily_budget_is_not_unlimited():
    """A fresh install must start bounded. 0 is how the bill ran away."""
    assert Settings().daily_token_budget > 0


def test_a_fresh_install_reports_a_task_and_call_limit():
    from app.llm.cost_guard import CostGuard

    guard = CostGuard(Settings().daily_token_budget)
    assert guard.task_limit() > 0
    assert guard.max_call > 0


@pytest.mark.asyncio
async def test_mode_shows_the_daily_limit(settings, store):
    from app.bot.handlers import TelegramBot
    from app.llm.cost_guard import CostGuard

    class _Msg:
        def __init__(self):
            self.texts = []

        async def reply_text(self, text, **kwargs):
            self.texts.append(text)
            return self

    class _Update:
        def __init__(self):
            self.effective_user = type("U", (), {"id": 111})()
            self.effective_message = _Msg()

    class _Orch:
        def __init__(self):
            self.cost_guard = CostGuard(150000)
            self.budget = None

    bot = TelegramBot(settings=settings, store=store, orchestrator=_Orch(), scheduler=None)
    update = _Update()
    await bot.cmd_mode(update, None)

    body = update.effective_message.texts[-1]
    assert "Batas harian" in body
    assert "150,000" in body, "the real ceiling must be printed, not a slogan"


@pytest.mark.asyncio
async def test_mode_warns_loudly_when_the_budget_is_unlimited(settings, store):
    """0 must be presented as a risk, not as a neutral setting."""
    from app.bot.handlers import TelegramBot
    from app.llm.cost_guard import CostGuard

    class _Msg:
        def __init__(self):
            self.texts = []

        async def reply_text(self, text, **kwargs):
            self.texts.append(text)
            return self

    class _Update:
        def __init__(self):
            self.effective_user = type("U", (), {"id": 111})()
            self.effective_message = _Msg()

    class _Orch:
        def __init__(self):
            self.cost_guard = CostGuard(0)
            self.budget = None

    bot = TelegramBot(settings=settings, store=store, orchestrator=_Orch(), scheduler=None)
    update = _Update()
    await bot.cmd_mode(update, None)

    body = update.effective_message.texts[-1]
    assert "tanpa batas" in body
    assert "DAILY_TOKEN_BUDGET=150000" in body, "the warning must say how to fix it"
