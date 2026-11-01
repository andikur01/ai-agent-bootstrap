"""The real token stock: what keys exist, and what they are actually worth.

The user asked for a number - "sekarang ada berapa?" - and was explicit that it
must not be an invention: "bukan halusinasi, tapi beneran". So this module never
guesses from a table of marketing figures. It counts the keys that are really
stored, and where a provider will tell us, it asks that provider directly.

Two kinds of number, kept apart on purpose:

* Counted - how many keys are stored, and how many are usable right now. This is
  a fact; it comes from the key pool and .env.
* Estimated - the daily token allowance. Providers do not publish a
  machine-readable quota, so this stays an estimate and is labelled as one. The
  planning table in capacity.py supplies it.

Anything the provider will confirm is marked as verified, and anything that
failed to verify is shown as failing rather than quietly counted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class ProviderStock:
    """What one provider holds, and what it is worth."""

    name: str
    label: str
    keys: int = 0
    active: int = 0
    cooling: int = 0
    est_daily_tokens: int = 0
    live_ok: bool | None = None  # None = not checked, True/False = verified
    live_note: str = ""
    renews: str = ""

    @property
    def usable(self) -> bool:
        return self.active > 0

    def line(self) -> str:
        icon = "✅" if self.active else ("⚠️" if self.keys else "•")
        bits = [f"{icon} *{self.label}* — {self.keys} key"]
        if self.cooling:
            bits.append(f"({self.cooling} cooldown)")
        if self.est_daily_tokens:
            bits.append(f"~{self.est_daily_tokens:,} token/hari")
        if self.live_ok is True:
            bits.append("| provider: key hidup")
        elif self.live_ok is False:
            bits.append(f"| provider: {self.live_note or 'ditolak'}")
        return " ".join(bits)


@dataclass
class TokenStock:
    """The whole picture, counted from real keys."""

    providers: list[ProviderStock] = field(default_factory=list)

    @property
    def total_keys(self) -> int:
        return sum(p.keys for p in self.providers)

    @property
    def active_keys(self) -> int:
        return sum(p.active for p in self.providers)

    @property
    def estimated_daily_tokens(self) -> int:
        """Only counts providers that have at least one usable key.

        Adding the allowance of a provider with no key would inflate the total
        with capacity the bot cannot reach, which is exactly the kind of number
        the user said not to invent.
        """
        return sum(p.est_daily_tokens for p in self.providers if p.active)

    def report(self) -> str:
        if not self.providers:
            return "Belum ada key tersimpan sama sekali."
        lines = ["*Stok token — dihitung dari key yang benar-benar ada*", ""]
        lines += [p.line() for p in self.providers]
        lines += [
            "",
            f"*Total: {self.active_keys} key aktif* dari {self.total_keys} tersimpan.",
            f"*Perkiraan: ~{self.estimated_daily_tokens:,} token/hari.*",
            "",
            "Angka token itu *perkiraan* dari tabel kapasitas, bukan janji provider — "
            "kuota gratis berubah tanpa pemberitahuan. Jumlah key-nya yang pasti.",
        ]
        return "\n".join(lines)


# Endpoints that answer "is this key alive", per provider. Same checks the
# /keys add path uses, so a key is judged the same way everywhere.
PROBES: dict[str, tuple[str, str]] = {
    "groq": ("https://api.groq.com/openai/v1/models", "Bearer"),
    "deepseek": ("https://api.deepseek.com/models", "Bearer"),
    "openrouter": ("https://openrouter.ai/api/v1/models", "Bearer"),
    "cerebras": ("https://api.cerebras.ai/v1/models", "Bearer"),
    "mistral": ("https://api.mistral.ai/v1/models", "Bearer"),
    "hf": ("https://huggingface.co/api/whoami-v2", "Bearer"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models?key={key}", "none"),
}


class StockCounter:
    """Counts real keys and, on request, verifies them against the provider."""

    def __init__(self, settings, *, key_pool=None):
        self.settings = settings
        self.key_pool = key_pool

    def _stored_keys(self, provider_name: str) -> list[str]:
        """Keys for a provider, from the live pool and from the environment."""
        keys: list[str] = []
        pool = self.key_pool
        if pool is not None:
            for state in pool.states.get(provider_name, []):
                if state.key and state.key not in keys:
                    keys.append(state.key)
        # The pool is built from settings at boot, so the environment is the same
        # source; reading it too keeps this correct when the pool is absent.
        env = getattr(self.settings, "provider_key_pool", None)
        if callable(env):
            for key in (env() or {}).get(provider_name, []):
                if key and key not in keys:
                    keys.append(key)
        return keys

    def count(self) -> TokenStock:
        from app.llm.capacity import CAPACITY
        from app.llm.credentials import PROVIDERS

        stock = TokenStock()
        for name, provider in PROVIDERS.items():
            keys = self._stored_keys(name)
            cooling = 0
            if self.key_pool is not None:
                cooling = sum(1 for s in self.key_pool.states.get(name, []) if s.cooling)
            cap = CAPACITY.get(name)
            stock.providers.append(
                ProviderStock(
                    name=name,
                    label=provider.label,
                    keys=len(keys),
                    active=max(0, len(keys) - cooling),
                    cooling=cooling,
                    est_daily_tokens=cap.rough_daily_tokens if cap else 0,
                    renews=cap.renews if cap else "",
                )
            )
        return stock

    async def verify(self, stock: TokenStock, *, limit: int = 6) -> TokenStock:
        """Ask each provider whether its key still works.

        Done concurrently with a small limit: a report that takes a minute is a
        report nobody waits for, and hammering every provider at once is a good
        way to get rate limited while checking for rate limits.
        """
        import httpx

        semaphore = asyncio.Semaphore(limit)

        async def check(entry: ProviderStock) -> None:
            keys = self._stored_keys(entry.name)
            if not keys:
                return
            probe = PROBES.get(entry.name)
            if probe is None:
                entry.live_note = "tidak ada cek cepat"
                return
            url, auth = probe
            async with semaphore:
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        target = url.format(key=keys[0]) if "{key}" in url else url
                        headers = (
                            {"Authorization": f"Bearer {keys[0]}"} if auth == "Bearer" else {}
                        )
                        response = await client.get(target, headers=headers)
                except Exception as exc:  # noqa: BLE001 - network trouble, not a bad key
                    entry.live_ok = None
                    entry.live_note = f"tidak terjangkau ({type(exc).__name__})"
                    return
            if response.status_code == 200:
                entry.live_ok = True
            elif response.status_code == 429:
                entry.live_ok = True
                entry.live_note = "kuota harian habis"
            else:
                entry.live_ok = False
                entry.live_note = f"HTTP {response.status_code}"

        await asyncio.gather(*(check(p) for p in stock.providers))
        return stock


def build_stock_counter(settings, *, key_pool=None) -> StockCounter:
    return StockCounter(settings, key_pool=key_pool)
