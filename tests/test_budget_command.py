"""`/budget` must let the user fix the ceiling from the chat.

Editing `.env` on a server is not something the user can do from a phone, and
that is the only place the ceiling used to live. These tests drive the real
handler and check the file on disk, not just the reply.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.llm.cost_guard import CostGuard
from app.llm.credentials import set_env_var


class _Msg:
    def __init__(self):
        self.texts: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        return self


class _Update:
    def __init__(self):
        self.effective_user = type("U", (), {"id": 111})()
        self.effective_message = _Msg()
        self.message = self.effective_message


class _Ctx:
    def __init__(self, args=None):
        self.args = args or []


class _Orch:
    def __init__(self, guard):
        self.cost_guard = guard
        self.budget = None


def _bot(settings, store, guard):
    from app.bot.handlers import TelegramBot

    return TelegramBot(settings=settings, store=store, orchestrator=_Orch(guard), scheduler=None)


def _settings_with_env(settings, tmp_path, monkeypatch):
    """Point the settings at a throwaway .env so the test never edits the real one."""
    env = tmp_path / ".env"
    env.write_text("DAILY_TOKEN_BUDGET=150000\n", encoding="utf-8")
    monkeypatch.setattr(type(settings), "env_file", property(lambda self: env))
    return env


@pytest.mark.asyncio
async def test_budget_with_no_argument_reports_the_ceiling(settings, store):
    guard = CostGuard(150000)
    bot = _bot(settings, store, guard)
    update = _Update()
    await bot.cmd_budget(update, _Ctx())
    body = update.effective_message.texts[-1]
    assert "150,000" in body


@pytest.mark.asyncio
async def test_budget_sets_a_new_ceiling_and_writes_env(settings, store, tmp_path, monkeypatch):
    env = _settings_with_env(settings, tmp_path, monkeypatch)
    guard = CostGuard(150000)
    bot = _bot(settings, store, guard)

    update = _Update()
    await bot.cmd_budget(update, _Ctx(["80000"]))

    assert guard.daily_budget == 80000, "the new ceiling must apply immediately"
    assert "DAILY_TOKEN_BUDGET=80000" in env.read_text(encoding="utf-8")
    assert "80,000" in update.effective_message.texts[-1]


@pytest.mark.asyncio
async def test_budget_off_is_allowed_but_warned(settings, store, tmp_path, monkeypatch):
    env = _settings_with_env(settings, tmp_path, monkeypatch)
    guard = CostGuard(150000)
    bot = _bot(settings, store, guard)

    update = _Update()
    await bot.cmd_budget(update, _Ctx(["off"]))

    assert guard.daily_budget == 0
    assert "DAILY_TOKEN_BUDGET=0" in env.read_text(encoding="utf-8")
    assert "⚠️" in update.effective_message.texts[-1]


@pytest.mark.asyncio
async def test_budget_refuses_a_value_too_small_to_finish_a_task(settings, store):
    guard = CostGuard(150000)
    bot = _bot(settings, store, guard)

    update = _Update()
    await bot.cmd_budget(update, _Ctx(["10"]))

    assert guard.daily_budget == 150000, "a tiny ceiling would abandon tasks mid-way"
    assert "Minimal" in update.effective_message.texts[-1]


@pytest.mark.asyncio
async def test_budget_rejects_nonsense(settings, store):
    guard = CostGuard(150000)
    bot = _bot(settings, store, guard)

    update = _Update()
    await bot.cmd_budget(update, _Ctx(["banyak"]))

    assert guard.daily_budget == 150000
    assert "angka" in update.effective_message.texts[-1].lower()


def test_set_env_var_replaces_without_duplicating(tmp_path):
    """Two /budget calls must not leave two DAILY_TOKEN_BUDGET lines."""
    env = tmp_path / ".env"
    env.write_text("A=1\nDAILY_TOKEN_BUDGET=150000\nB=2\n", encoding="utf-8")

    set_env_var(env, "DAILY_TOKEN_BUDGET", "90000")
    set_env_var(env, "DAILY_TOKEN_BUDGET", "70000")

    body = env.read_text(encoding="utf-8")
    assert body.count("DAILY_TOKEN_BUDGET=") == 1
    assert "DAILY_TOKEN_BUDGET=70000" in body
    assert "A=1" in body and "B=2" in body, "other settings must survive"


def test_set_env_var_appends_when_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1", encoding="utf-8")  # no trailing newline
    set_env_var(env, "DAILY_TOKEN_BUDGET", "12345")
    body = env.read_text(encoding="utf-8")
    assert "A=1\n" in body
    assert "DAILY_TOKEN_BUDGET=12345" in body
