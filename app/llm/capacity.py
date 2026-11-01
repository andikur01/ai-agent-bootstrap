"""How much free capacity a provider gives, and how to get more of it.

The user's goal is "the bot grows its own token supply, and I stop doing
manual work". This module is the honest version of that: it knows, per
provider, how many keys an account can hold, roughly how many tokens or
requests that buys per day, and the exact page where the next key is created.

That turns an exhausted quota into an actionable message instead of a silent
failure:

    "Semua key Gemini sedang cooldown. Buka <link>, bikin key #3, kirim ke sini."

The bot cannot create the key itself - providers expose no API for that, and
automating account signup gets accounts banned. What it can do is decide *when*
a new key is needed and remove every step except the click.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capacity:
    provider: str
    keys_per_account: int
    daily_note: str
    rough_daily_tokens: int
    renews: str


# Figures are order-of-magnitude planning numbers, not guarantees; free tiers
# change without notice. They are here to answer "do I have enough for today".
CAPACITY: dict[str, Capacity] = {
    "gemini": Capacity(
        provider="gemini",
        keys_per_account=10,
        daily_note="~1500 request/hari per project, beberapa project per akun",
        rough_daily_tokens=1_000_000,
        renews="tiap hari, reset tengah malam PT",
    ),
    "cerebras": Capacity(
        provider="cerebras",
        keys_per_account=5,
        daily_note="~1 juta token/hari, paling besar di antara yang gratis",
        rough_daily_tokens=1_000_000,
        renews="tiap hari",
    ),
    "groq": Capacity(
        provider="groq",
        keys_per_account=10,
        daily_note="limit harian per model, cepat tapi cepat habis",
        rough_daily_tokens=500_000,
        renews="tiap hari",
    ),
    "deepseek": Capacity(
        provider="deepseek",
        keys_per_account=10,
        daily_note="berbayar murah; ada bonus awal, tanpa free harian tetap",
        rough_daily_tokens=100_000,
        renews="kredit, bukan harian",
    ),
    "openrouter": Capacity(
        provider="openrouter",
        keys_per_account=10,
        daily_note="banyak model :free, limit per hari per akun",
        rough_daily_tokens=200_000,
        renews="tiap hari",
    ),
    "mistral": Capacity(
        provider="mistral",
        keys_per_account=5,
        daily_note="tier gratis ada, limit per menit",
        rough_daily_tokens=150_000,
        renews="tiap bulan",
    ),
    "hf": Capacity(
        provider="hf",
        keys_per_account=10,
        daily_note="gratis, dipakai untuk LLM + gambar FLUX",
        rough_daily_tokens=100_000,
        renews="tiap bulan (kredit inference)",
    ),
}

# What the user should do when a provider's keys are all cooling down.
ACQUIRE_STEPS = (
    "1. Buka {console} (login kamu sendiri, sekali saja)",
    "2. Klik 'Create new secret key' / 'Create API key'",
    "3. Copy, lalu kirim ke sini: /keys add {provider} <key>",
)


def capacity_for(provider: str) -> Capacity | None:
    return CAPACITY.get(provider)


def total_daily_tokens() -> int:
    """Sum of every provider's rough daily capacity, for planning only."""
    return sum(c.rough_daily_tokens for c in CAPACITY.values())


def estimate_message(tokens_per_task: int, tasks_per_day: int) -> dict:
    """Compare a usage plan against the free capacity, in round numbers."""
    needed = max(1, tokens_per_task) * max(1, tasks_per_day)
    available = total_daily_tokens()
    return {
        "needed": needed,
        "available": available,
        "fits": needed <= available,
        "providers_needed": max(1, -(-needed // max(1, available // 7))),
    }


def acquire_message(provider: str, console_url: str) -> str:
    steps = "\n".join(
        line.format(console=console_url, provider=provider) for line in ACQUIRE_STEPS
    )
    cap = CAPACITY.get(provider)
    extra = ""
    if cap:
        extra = (
            f"\n\nProvider ini kasih {cap.daily_note}. "
            f"Kuota {cap.renews}. Satu akun bisa sampai {cap.keys_per_account} key, "
            "jadi kalau satu habis tinggal bikin berikutnya."
        )
    return f"🔑 Key baru dibutuhkan untuk *{provider}*.\n\n{steps}{extra}"
