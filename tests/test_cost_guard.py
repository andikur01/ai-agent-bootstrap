"""Cost control: the guard must refuse before spending, not report after."""
from __future__ import annotations

import pytest

from app.llm.cost_guard import (
    DEFAULT_MAX_CALL_TOKENS,
    BudgetExceeded,
    CostGuard,
    format_usage,
)


def test_unlimited_budget_warns_once():
    guard = CostGuard(0)
    first = guard.unlimited_warning()
    assert "tanpa batas" in first
    assert guard.unlimited_warning() == ""


def test_configured_budget_does_not_warn():
    assert CostGuard(100000).unlimited_warning() == ""


def test_daily_budget_blocks_the_call_that_would_overshoot():
    guard = CostGuard(10000)
    with pytest.raises(BudgetExceeded) as caught:
        guard.check(wanted=2000, day_used=9000)
    assert caught.value.scope == "day"
    assert caught.value.limit == 10000


def test_daily_budget_allows_a_call_that_fits():
    guard = CostGuard(10000)
    guard.check(wanted=500, day_used=1000)  # must not raise


def test_task_ceiling_stops_a_runaway_loop():
    """One stubborn task must not be able to eat the whole day."""
    guard = CostGuard(100000)
    task = guard.start_task("web_repair", estimate=5000)
    task.record(task.limit)  # spend the ceiling
    with pytest.raises(BudgetExceeded) as caught:
        guard.check(wanted=100, task=task)
    assert caught.value.scope == "task"


def test_a_single_enormous_call_is_refused():
    guard = CostGuard(0)  # unlimited day, still bounded per call
    with pytest.raises(BudgetExceeded) as caught:
        guard.check(wanted=DEFAULT_MAX_CALL_TOKENS + 1)
    assert caught.value.scope == "call"


def test_task_summary_reports_calls_and_spend():
    guard = CostGuard(100000)
    task = guard.start_task("chat")
    task.record(300, note="groq/small")
    task.record(200)
    assert task.calls == 2
    assert task.spent == 500
    assert "500" in task.summary()


def test_budget_message_names_the_limit_and_how_to_raise_it():
    exc = BudgetExceeded("day", 50000, 49000, 5000)
    message = exc.user_message()
    assert "harian" in message
    assert "DAILY_TOKEN_BUDGET" in message


def test_estimate_shrinks_the_task_ceiling():
    """A cheap task should not get a heavy task's allowance."""
    guard = CostGuard(1_000_000)
    cheap = guard.start_task("chat", estimate=1000)
    heavy = guard.start_task("web_static", estimate=20000)
    assert cheap.limit < heavy.limit


def test_format_usage_shows_percent_only_with_a_budget():
    usage = {"total_tokens": 5000, "prompt_tokens": 4000, "completion_tokens": 1000}
    with_budget = format_usage(usage, 10000)
    assert "50%" in with_budget
    without = format_usage(usage, 0)
    assert "tanpa batas" in without
