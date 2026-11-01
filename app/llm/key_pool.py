"""Several keys per provider, rotated when one runs out.

Free tiers run dry. A single Gemini key gives 1500 requests a day, then every
call fails until midnight. If the user supplies three keys, the bot should
simply move to the next one and keep answering - not go silent.

Keys are read from numbered env vars: ``GEMINI_API_KEY_1``, ``_2``, ``_3``. The
plain ``GEMINI_API_KEY`` is treated as key 1, so an existing .env keeps working
without edits.

State (which key is active, which are cooling down) is kept in a small JSON file
so a restart does not immediately hammer a key that just returned 429.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

# How long a key stays benched after the provider says it is out of quota.
DEFAULT_COOLDOWN_SECONDS = 60

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "too many requests",
    "overloaded",
)


def looks_like_rate_limit(error: Exception | None) -> bool:
    """True when the provider is throttling us rather than rejecting the key."""
    if error is None:
        return False
    blob = f"{type(error).__name__} {error}".lower()
    return any(marker in blob for marker in _RATE_LIMIT_MARKERS)


@dataclass
class KeyState:
    provider: str
    slot: int
    key: str
    cooldown_until: float = 0.0
    uses: int = 0
    last_error: str = ""

    @property
    def cooling(self) -> bool:
        return self.cooldown_until > time.time()

    def mask(self) -> str:
        """Never show a key in full - last 4 chars are enough to tell them apart."""
        if len(self.key) <= 4:
            return "****"
        return f"****{self.key[-4:]}"

    def label(self) -> str:
        return f"{self.provider.upper()}_{self.slot}"


@dataclass
class KeyPool:
    """Rotating keys per provider, with cooldowns and a usage tally."""

    states: dict[str, list[KeyState]] = field(default_factory=dict)
    state_file: Path | None = None
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS

    # ------------------------------------------------------------- loading
    @classmethod
    def from_settings(cls, settings, state_file: Path | None = None) -> KeyPool:
        pool = cls(state_file=state_file, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS)
        for provider, keys in settings.provider_key_pool().items():
            pool.states[provider] = [
                KeyState(provider=provider, slot=i + 1, key=key)
                for i, key in enumerate(keys)
            ]
        pool._load()
        return pool

    def _load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - corrupt state must not block startup
            log.warning("key pool state unreadable, starting fresh: %s", exc)
            return
        for provider, saved in (raw.get("providers") or {}).items():
            for state in self.states.get(provider, []):
                entry = saved.get(str(state.slot))
                if entry:
                    state.cooldown_until = float(entry.get("cooldown_until", 0))
                    state.uses = int(entry.get("uses", 0))

    def _save(self) -> None:
        if not self.state_file:
            return
        payload = {
            "providers": {
                provider: {
                    str(s.slot): {"cooldown_until": s.cooldown_until, "uses": s.uses}
                    for s in states
                }
                for provider, states in self.states.items()
            }
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("key pool state not saved: %s", exc)

    # ------------------------------------------------------------- rotating
    def has(self, provider: str) -> bool:
        return bool(self.states.get(provider))

    def active(self, provider: str) -> KeyState | None:
        """The next usable key: prefer one that is not cooling down."""
        states = self.states.get(provider)
        if not states:
            return None
        for state in states:
            if not state.cooling:
                return state
        # Everything is benched. Return the one that frees up soonest rather
        # than failing outright - a stale cooldown is better than no attempt.
        return min(states, key=lambda s: s.cooldown_until)

    def rotate(self, provider: str, error: Exception | None = None) -> KeyState | None:
        """Bench the active key and hand back the next one."""
        current = self.active(provider)
        if current is None:
            return None
        current.cooldown_until = time.time() + self.cooldown_seconds
        current.last_error = str(error)[:160] if error else "rate limited"
        log.info("key pool: %s benched for %ss", current.label(), self.cooldown_seconds)
        self._save()
        return self.active(provider)

    def record_use(self, provider: str) -> None:
        current = self.active(provider)
        if current is not None:
            current.uses += 1

    def slots(self, provider: str) -> int:
        return len(self.states.get(provider, []))

    def providers(self) -> list[str]:
        return list(self.states)

    # -------------------------------------------------------------- display
    def status_lines(self) -> list[str]:
        """One line per key, for the /keys command."""
        out: list[str] = []
        now = time.time()
        for provider, states in self.states.items():
            for state in states:
                if state.cooling:
                    left = int(state.cooldown_until - now)
                    out.append(f"⚠️ `{state.label()}` {state.mask()} — cooldown {left}s")
                else:
                    out.append(f"✅ `{state.label()}` {state.mask()} — aktif, {state.uses} pakai")
        return out or ["Belum ada key tambahan di pool."]


def build_key_pool(settings, state_file: Path | None = None) -> KeyPool:
    return KeyPool.from_settings(settings, state_file=state_file)
