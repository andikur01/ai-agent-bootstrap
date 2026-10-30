"""Tiny async helpers shared across modules."""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


async def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking callable in the default executor."""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, call)


async def gather_limited(
    coros: list[Awaitable[T]], limit: int = 4
) -> list[T | BaseException]:
    """Gather with bounded concurrency, returning exceptions instead of raising."""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def guarded(coro: Awaitable[T]) -> T | BaseException:
        async with semaphore:
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 - deliberate: collect per-task
                return exc

    return await asyncio.gather(*(guarded(c) for c in coros))


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix
