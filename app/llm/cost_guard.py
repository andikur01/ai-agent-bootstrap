"""Cost control: make the token bill predictable instead of surprising.

The user's complaint was concrete - a million tokens in a day - and the cause
was equally concrete: the daily budget defaulted to unlimited, so nothing ever
said "enough". A guard that only reports the total after the fact is not a
guard; it is an autopsy. This one refuses the next call before it is made.

Three separate limits, because they stop three different failures:

* **Daily budget** - the whole day across every task. Stops a slow leak from
  becoming a surprise.
* **Per-task budget** - one task cannot eat the day. Stops a repair loop that
  keeps retrying a site that will never pass.
* **Call guard** - a single call cannot be enormous. Stops one runaway prompt.

The numbers are deliberately separate from the *estimates* shown to the user
before heavy work. An estimate is a promise about cost; a budget is a ceiling
that holds even when the estimate was wrong. When a limit is hit, the message
says which limit and what it was doing, because "quota habis" with no reason is
the thing that makes a user distrust the bot.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging_setup import get_logger

log = get_logger(__name__)

# A single call above this is almost always a mistake - a whole file pasted
# into a prompt, or a loop feeding its own output back. Refusing it costs one
# retry; allowing it costs the day.
DEFAULT_MAX_CALL_TOKENS = 24000

# Fraction of the daily budget one task may consume. The rest stays for chat
# and for the next request, so a single heavy job cannot lock the user out.
TASK_SHARE = 0.6


class BudgetExceeded(Exception):
    """Raised before an expensive call, never after."""

    def __init__(self, scope: str, limit: int, used: int, wanted: int):
        self.scope = scope
        self.limit = limit
        self.used = used
        self.wanted = wanted
        super().__init__(f"{scope}: {used + wanted:,} > {limit:,}")

    def user_message(self) -> str:
        where = {
            "day": "batas token harian",
            "task": "batas token untuk satu tugas ini",
            "call": "batas token satu panggilan",
        }.get(self.scope, "batas token")
        return (
            f"⛔ {where} sudah tercapai.\n"
            f"  terpakai: {self.used:,} token\n"
            f"  butuh: ~{self.wanted:,} token lagi\n"
            f"  batas: {self.limit:,} token\n\n"
            "Ini pengaman biaya, bukan error. Kamu bisa naikkan lewat "
            "`DAILY_TOKEN_BUDGET` di `.env`, atau lanjut besok."
        )


@dataclass
class TaskSpend:
    """Running total for one task, so a loop cannot hide its own cost."""

    scope: str
    limit: int
    spent: int = 0
    calls: int = 0
    history: list[str] = field(default_factory=list)

    def can_spend(self, wanted: int) -> bool:
        return self.limit <= 0 or self.spent + wanted <= self.limit

    def record(self, tokens: int, note: str = "") -> None:
        self.spent += max(0, tokens)
        self.calls += 1
        if note:
            self.history.append(f"{note}: {tokens:,}")

    def summary(self) -> str:
        if self.limit <= 0:
            return f"{self.calls} panggilan, {self.spent:,} token"
        return f"{self.calls} panggilan, {self.spent:,}/{self.limit:,} token"


class CostGuard:
    """Decides whether a call may happen, and remembers what it cost.

    ``daily_budget`` of 0 means unlimited, which is what the user had before and
    is why the bill ran away. The bot warns once when it is unlimited so the
    choice is visible rather than accidental.
    """

    def __init__(self, daily_budget: int, *, max_call: int = DEFAULT_MAX_CALL_TOKENS):
        self.daily_budget = max(0, daily_budget)
        self.max_call = max(0, max_call)
        self._warned_unlimited = False

    def task_limit(self) -> int:
        if self.daily_budget <= 0:
            return 0
        return max(1000, int(self.daily_budget * TASK_SHARE))

    def start_task(self, scope: str, estimate: int = 0) -> TaskSpend:
        limit = self.task_limit()
        if estimate > 0 and limit > 0:
            limit = min(limit, max(estimate * 3, 2000))
        return TaskSpend(scope=scope, limit=limit)

    def check(
        self,
        *,
        wanted: int,
        day_used: int = 0,
        task: TaskSpend | None = None,
    ) -> None:
        """Raise ``BudgetExceeded`` when the call should not happen."""
        wanted = max(0, wanted)

        if self.max_call and wanted > self.max_call:
            raise BudgetExceeded("call", self.max_call, 0, wanted)

        if task is not None and not task.can_spend(wanted):
            raise BudgetExceeded("task", task.limit, task.spent, wanted)

        if self.daily_budget and day_used + wanted > self.daily_budget:
            raise BudgetExceeded("day", self.daily_budget, day_used, wanted)

    def unlimited_warning(self) -> str:
        """One-time nudge, so an unlimited budget is a decision, not a default."""
        if self.daily_budget > 0 or self._warned_unlimited:
            return ""
        self._warned_unlimited = True
        return (
            "ℹ️ Pengaman biaya belum diatur (`DAILY_TOKEN_BUDGET=0` = tanpa batas).\n"
            "Isi di `.env`, misalnya `DAILY_TOKEN_BUDGET=200000`, biar token tidak "
            "kebablasan lagi. Sementara ini aku tetap pasang batas per tugas "
            f"({self.max_call:,} token per panggilan)."
        )


def format_usage(usage: dict, budget: int = 0) -> str:
    """A short, honest line about what was spent - for when the user asks."""
    total = int(usage.get("total_tokens", 0))
    limit = f"{budget:,}" if budget > 0 else "tanpa batas"
    percent = f" ({100 * total // budget}%)" if budget > 0 else ""
    lines = [
        f"Token hari ini: *{total:,}*{percent} dari {limit}",
        f"  masuk: {int(usage.get('prompt_tokens', 0)):,} | keluar: {int(usage.get('completion_tokens', 0)):,}",
    ]
    if usage.get("cached_hits"):
        lines.append(f"  hemat dari cache: {int(usage['cached_hits']):,} jawaban")
    return "\n".join(lines)
