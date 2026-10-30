"""Daily token budget guard.

Cheap insurance against a runaway loop burning a whole month of quota in one
afternoon. Counters live in SQLite so they survive restarts and are shared
between the bot process and any worker.
"""
from __future__ import annotations

from app.core.errors import BudgetExceededError
from app.db.store import Store


class BudgetGuard:
    def __init__(self, store: Store, daily_budget: int):
        self.store = store
        self.daily_budget = max(0, daily_budget)

    async def usage(self, user_id: int) -> dict:
        return await self.store.usage_today(user_id)

    async def remaining(self, user_id: int) -> int:
        if self.daily_budget <= 0:
            return 10**9
        used = (await self.usage(user_id))["total_tokens"]
        return max(0, self.daily_budget - used)

    async def check(self, user_id: int, estimated_tokens: int) -> None:
        """Raise ``BudgetExceededError`` when the estimate cannot fit today."""
        if self.daily_budget <= 0:
            return
        used = (await self.usage(user_id))["total_tokens"]
        if used + estimated_tokens > self.daily_budget:
            raise BudgetExceededError(
                f"Budget harian {self.daily_budget:,} token, terpakai {used:,}, "
                f"butuh ~{estimated_tokens:,}",
            )

    async def record(
        self,
        user_id: int,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_hits: int = 0,
    ) -> None:
        await self.store.add_usage(
            user_id, model or "unknown", prompt_tokens, completion_tokens, cached_hits
        )

    async def summary(self, user_id: int) -> str:
        usage = await self.usage(user_id)
        limit = f"{self.daily_budget:,}" if self.daily_budget > 0 else "unlimited"
        lines = [
            f"Token hari ini: *{usage['total_tokens']:,}* / {limit}",
            f"  in: {usage['prompt_tokens']:,} | out: {usage['completion_tokens']:,}",
            f"  hemat dari cache: {usage['cached_hits']:,} panggilan",
        ]
        if usage["per_model"]:
            lines.append("Per model:")
            for row in usage["per_model"]:
                total = int(row["prompt_tokens"]) + int(row["completion_tokens"])
                lines.append(f"  - `{row['model']}`: {total:,}")
        return "\n".join(lines)
