"""Token counting and prompt budgeting with tiktoken.

We never send an unbounded prompt. Everything is measured first, trimmed to a
budget, and the cost is reported back to the caller so the user can be told
what a big task is going to cost before it runs.
"""
from __future__ import annotations

from dataclasses import dataclass

try:  # tiktoken is optional at import time (needs to download BPE files once)
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - offline fallback
    _ENCODER = None


def count_tokens(text: str) -> int:
    """Token count for *text*. Falls back to a ~4 chars/token heuristic."""
    if not text:
        return 0
    if _ENCODER is not None:
        try:
            return len(_ENCODER.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def count_message_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += 4  # per-message overhead
        for value in message.values():
            if isinstance(value, str):
                total += count_tokens(value)
    return total + 2


def trim_to_tokens(text: str, max_tokens: int, *, keep_tail: bool = False) -> str:
    """Hard-trim *text* so it fits in *max_tokens*."""
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text
    if _ENCODER is None:
        approx_chars = max_tokens * 4
        return text[-approx_chars:] if keep_tail else text[:approx_chars]
    ids = _ENCODER.encode(text)
    kept = ids[-max_tokens:] if keep_tail else ids[:max_tokens]
    return _ENCODER.decode(kept)


def trim_messages(
    messages: list[dict],
    max_tokens: int,
    *,
    keep_system: bool = True,
    keep_last: int = 4,
) -> list[dict]:
    """Drop the oldest non-system messages until the prompt fits the budget."""
    if count_message_tokens(messages) <= max_tokens:
        return list(messages)

    system = [m for m in messages if m.get("role") == "system"] if keep_system else []
    others = [m for m in messages if m not in system] if keep_system else list(messages)

    tail = others[-keep_last:] if keep_last > 0 else []
    head_pool = others[: len(others) - len(tail)] if tail else others

    result = system + tail
    for message in reversed(head_pool):
        candidate = system + [message] + tail
        if count_message_tokens(candidate) <= max_tokens:
            result = candidate
        else:
            break

    # Still too big: trim the last user message content itself.
    if count_message_tokens(result) > max_tokens:
        for message in reversed(result):
            if message.get("role") == "user":
                overhead = count_message_tokens(result) - count_tokens(message.get("content", ""))
                message["content"] = trim_to_tokens(
                    message.get("content", ""), max(64, max_tokens - overhead), keep_tail=True
                )
                break
    return result


@dataclass
class TokenEstimate:
    prompt_tokens: int
    expected_completion_tokens: int
    model: str

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.expected_completion_tokens

    def report(self) -> str:
        return (
            f"Estimasi: ~{self.prompt_tokens:,} token input + "
            f"~{self.expected_completion_tokens:,} token output "
            f"= ~{self.total:,} token pakai `{self.model}`"
        )


def estimate(messages: list[dict], model: str, expected_completion: int = 800) -> TokenEstimate:
    return TokenEstimate(
        prompt_tokens=count_message_tokens(messages),
        expected_completion_tokens=expected_completion,
        model=model,
    )
