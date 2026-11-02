"""Tests for the per-task spending scope.

Every chat crashed with "'function' object has no attribute 'set'" because
``bind_task`` called ``.set`` on a factory instead of returning its context
manager. Nothing tested it, so it reached the user. These tests exist so that
cannot happen again.
"""
from __future__ import annotations

import pytest

from app.llm.gateway import _CURRENT_TASK, LLMGateway
from app.llm.budget import BudgetGuard


@pytest.fixture()
def gw(settings, store):
    return LLMGateway(settings, budget=BudgetGuard(store, settings.daily_token_budget))


def test_bind_task_returns_a_usable_context_manager(gw):
    """The regression: bind_task must give something with __enter__/__exit__."""
    binding = gw.bind_task(object())

    assert hasattr(binding, "__enter__"), "bind_task must return a context manager"
    assert hasattr(binding, "__exit__"), "bind_task must return a context manager"


def test_bind_task_scopes_the_current_task_and_restores_it(gw):
    """Inside the block the task is set; outside it, the previous value is back."""
    task = object()
    assert _CURRENT_TASK.get() is None

    with gw.bind_task(task):
        assert _CURRENT_TASK.get() is task

    assert _CURRENT_TASK.get() is None, "the scope must be popped, not left set"


def test_bind_task_restores_the_outer_task_when_nested(gw):
    """A sub-task inside a task must not clobber the outer one."""
    outer = object()
    inner = object()

    with gw.bind_task(outer):
        assert _CURRENT_TASK.get() is outer
        with gw.bind_task(inner):
            assert _CURRENT_TASK.get() is inner
        assert _CURRENT_TASK.get() is outer


def test_bind_task_does_not_leak_across_tasks(gw):
    """Two concurrent tasks must not see each other's binding."""
    import asyncio

    seen = {}

    async def worker(name, task):
        with gw.bind_task(task):
            await asyncio.sleep(0.01)
            seen[name] = _CURRENT_TASK.get()

    async def scenario():
        a, b = object(), object()
        await asyncio.gather(worker("a", a), worker("b", b))
        return a, b

    a, b = asyncio.run(scenario())
    assert seen["a"] is a
    assert seen["b"] is b
