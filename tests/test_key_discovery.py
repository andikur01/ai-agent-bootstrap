"""Finding keys on any site, and counting what is really stored.

Two claims the user asked to have proven rather than asserted:

* the agent can go to a site nobody hard-coded and read keys from it, and
* the token stock it reports is counted, not invented.

These tests hold both claims to the same standard as the rest of the suite.
"""
from __future__ import annotations

import pytest

from app.services.key_discovery import (
    DiscoveryResult,
    FoundKey,
    looks_like_key,
    scan_text_for_keys,
)

GEMINI_KEY = "AIzaSyD" + "x" * 32
GROQ_KEY = "gsk_" + "a" * 40
OPENROUTER_KEY = "sk-or-v1-" + "a" * 64
HF_KEY = "hf_" + "a" * 34
CEREBRAS_KEY = "csk-" + "a" * 40
REPLICATE_KEY = "r8_" + "a" * 32


# ---------------------------------------------------------------------------
# Recognising a key. The failure that matters is a false positive: a session id
# or a hash saved into .env as a credential, which then looks like capacity the
# bot does not have.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,kind",
    [
        (GEMINI_KEY, "gemini"),
        (GROQ_KEY, "groq"),
        (OPENROUTER_KEY, "openrouter"),
        (HF_KEY, "huggingface"),
        (CEREBRAS_KEY, "cerebras"),
        (REPLICATE_KEY, "replicate"),
    ],
)
def test_a_real_key_shape_is_recognised(value, kind):
    hit = looks_like_key(value)
    assert hit is not None, f"{kind} key not recognised"
    assert hit[0] == kind


@pytest.mark.parametrize(
    "value",
    [
        "",
        "hello",
        "password123",
        "user@example.com",
        # Too short to be any provider's key.
        "AIzaSyD-short",
        # A key embedded in a longer string is not a key on its own.
        f"token={GEMINI_KEY}&expires=3600",
    ],
)
def test_things_that_are_not_keys_are_rejected(value):
    assert looks_like_key(value) is None


def test_an_unlabelled_hex_string_needs_context():
    """A bare 64-char hex run is usually a hash, not a credential."""
    bare = "a" * 64
    assert scan_text_for_keys(f"cache key {bare} stored") == []
    with_context = scan_text_for_keys(f"Your API key: {bare}")
    assert with_context, "a hex key next to the words 'API key' should be reported"


def test_a_page_full_of_hashes_yields_nothing():
    page = "\n".join(f"session {i}: {'b' * 64}" for i in range(20))
    assert scan_text_for_keys(page) == []


def test_scan_finds_several_providers_at_once():
    page = f"Gemini: {GEMINI_KEY}\nGroq: {GROQ_KEY}\nHF: {HF_KEY}"
    found = dict((kind, value) for kind, value in scan_text_for_keys(page))
    assert found["gemini"] == GEMINI_KEY
    assert found["groq"] == GROQ_KEY
    assert found["huggingface"] == HF_KEY


def test_a_key_is_not_reported_twice():
    page = f"{GEMINI_KEY} and again {GEMINI_KEY}"
    values = [value for _, value in scan_text_for_keys(page)]
    assert values.count(GEMINI_KEY) == 1


# ---------------------------------------------------------------------------
# What the user is shown
# ---------------------------------------------------------------------------


def test_the_summary_shows_the_values_not_just_a_count():
    """The point of discovery is to stop the user copying by hand, so the value
    has to be in the message."""
    result = DiscoveryResult(
        ok=True,
        url="https://example.com",
        host="example.com",
        keys=[FoundKey(value=GROQ_KEY, kind="groq", host="example.com")],
    )
    summary = result.summary()
    assert GROQ_KEY in summary
    assert "groq" in summary


def test_masking_keeps_the_ends_and_hides_the_middle():
    found = FoundKey(value=GEMINI_KEY, kind="gemini", host="example.com")
    masked = found.masked()
    assert GEMINI_KEY not in masked
    assert masked.startswith(GEMINI_KEY[:6])


def test_an_empty_result_says_so_plainly():
    result = DiscoveryResult(ok=True, url="https://example.com", host="example.com")
    assert "Tidak ada key" in result.summary()


# ---------------------------------------------------------------------------
# Routing: the user names a site, the agent goes there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ambil key di somekey.com", "site:https://somekey.com"),
        ("ambil api key dari https://newkeys.io/tokens", "site:https://newkeys.io/tokens"),
        ("kasih aku key di toge.ai", "site:https://toge.ai"),
        ("ambil semua key", "all"),
        ("ambilkan key gemini", "gemini"),
        # Creating an account stays manual - that is a hard rule, not a limit.
        ("bikin akun baru di x.com", None),
        ("tolong rangkum artikel ini", None),
    ],
)
def test_a_named_site_routes_to_discovery(text, expected):
    from app.bot.handlers import _parse_harvest_request

    assert _parse_harvest_request(text) == expected


@pytest.mark.parametrize(
    "text,expected_count",
    [
        (f"simpan {GROQ_KEY}", 1),
        (f"simpan {GROQ_KEY} {HF_KEY}", 2),
        (f"pasang {GEMINI_KEY}", 1),
        ("simpan passwordku123", 0),
        ("simpan semua", 0),
        ("simpan", 0),
    ],
)
def test_only_key_shaped_values_can_be_saved(text, expected_count):
    """A message prefixed with "simpan" must not become a way to write an
    arbitrary string - a password, say - into a credential slot."""
    from app.bot.handlers import _parse_save_request

    values = _parse_save_request(text)
    assert len(values or []) == expected_count


# ---------------------------------------------------------------------------
# The stock report. The count is a fact; the allowance is an estimate and must
# say so, because the user asked for no hallucinated numbers.
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, name, label):
        self.name = name
        self.label = label


def _stock_with(monkeypatch, keys_by_provider: dict[str, list[str]]):
    from app.llm.stock import StockCounter

    class FakeSettings:
        def provider_key_pool(self):
            return keys_by_provider

    counter = StockCounter(FakeSettings(), key_pool=None)
    monkeypatch.setattr(counter, "_stored_keys", lambda name: keys_by_provider.get(name, []))
    return counter


def test_the_count_comes_from_stored_keys(monkeypatch):
    counter = _stock_with(monkeypatch, {"groq": [GROQ_KEY, GROQ_KEY], "hf": [HF_KEY]})
    stock = counter.count()

    by_name = {p.name: p for p in stock.providers}
    assert by_name["groq"].keys == 2
    assert by_name["hf"].keys == 1
    assert by_name["gemini"].keys == 0
    assert stock.total_keys == 3
    assert stock.active_keys == 3


def test_capacity_is_only_counted_for_providers_that_have_a_key(monkeypatch):
    """Adding the allowance of a provider with no key would inflate the total
    with capacity the bot cannot reach - exactly the invented number the user
    said not to produce."""
    counter = _stock_with(monkeypatch, {"groq": [GROQ_KEY]})
    stock = counter.count()

    total = stock.estimated_daily_tokens
    only_groq = next(p for p in stock.providers if p.name == "groq").est_daily_tokens
    assert total == only_groq
    assert total < sum(p.est_daily_tokens for p in stock.providers)


def test_an_empty_stock_says_there_is_nothing():
    from app.llm.stock import TokenStock

    assert "Belum ada key" in TokenStock().report()


def test_the_report_labels_the_estimate_as_an_estimate(monkeypatch):
    counter = _stock_with(monkeypatch, {"groq": [GROQ_KEY]})
    report = counter.count().report()
    assert "perkiraan" in report.lower()
    assert "key" in report.lower()


def test_a_cooling_key_is_not_counted_as_active(monkeypatch):
    from app.llm.key_pool import KeyPool, KeyState

    counter = _stock_with(monkeypatch, {"groq": [GROQ_KEY]})
    pool = KeyPool(state_file=None)
    pool.states["groq"] = [KeyState(provider="groq", slot=1, key=GROQ_KEY, cooldown_until=9e9)]
    counter.key_pool = pool

    stock = counter.count()
    groq = next(p for p in stock.providers if p.name == "groq")
    assert groq.keys == 1
    assert groq.active == 0
    assert groq.cooling == 1
    assert stock.estimated_daily_tokens == 0, "a benched key is not capacity right now"


def test_a_session_id_on_a_credentials_page_is_not_a_key():
    """Found against a real page: a page titled "credentials" made the scanner
    report its session id as a key. A wrong key in .env is worse than a missed
    one - it looks like capacity the bot does not have."""
    page = (
        "Developer API Keys\n"
        "Your new API key is ready.\n"
        "Other credentials\n"
        "Session: 7f3ab9c2e1d4a8b6c0f5e2d9a1b4c7e8f0a2d4b6c8e0f2a4b6c8d0e2f4a6b8c0\n"
    )
    values = [value for _, value in scan_text_for_keys(page)]
    assert not any(value.startswith("7f3ab9") for value in values)


def test_a_hash_next_to_the_word_hash_is_not_a_key():
    page = f"Cache hash: {'d' * 64}\nRequest id: {'e' * 64}"
    assert scan_text_for_keys(page) == []


def test_a_prefixed_key_is_still_found_on_a_credentials_page():
    """The guard must not throw out the real key along with the session id."""
    page = (
        "API key\n"
        f"{GROQ_KEY}\n"
        f"Session: {'a' * 64}\n"
    )
    values = [value for _, value in scan_text_for_keys(page)]
    assert GROQ_KEY in values


# ---------------------------------------------------------------------------
# The save step, end to end. The guard that matters: a value the provider
# rejects must not be written, and a value that is not key-shaped must never
# reach .env at all.
# ---------------------------------------------------------------------------


class _FakeProvider2:
    name = "groq"
    label = "Groq"
    env_var = "GROQ_API_KEY"


@pytest.mark.asyncio
async def test_a_rejected_key_is_not_written(tmp_path, monkeypatch):
    """An unusable key in .env looks like capacity that does not exist."""
    from app.bot import handlers as H

    target = tmp_path / ".env"
    target.write_text("GROQ_API_KEY=existing\n", encoding="utf-8")

    class FakeSettings:
        env_file = str(target)

    class FakeGateway:
        key_pool = None

    class FakeMessage:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kwargs):
            self.sent.append(text)

    class FakeUpdate:
        def __init__(self):
            self.effective_message = FakeMessage()

    bot = H.TelegramBot.__new__(H.TelegramBot)
    bot.settings = FakeSettings()
    bot.orch = type("O", (), {"gateway": FakeGateway()})()

    monkeypatch.setattr(H, "guess_provider_from_key", None, raising=False)
    monkeypatch.setattr(
        bot, "_probe_key", lambda provider, key: _async({"ok": False, "reason": "HTTP 401"})
    )

    import app.llm.credentials as creds

    monkeypatch.setattr(creds, "guess_provider_from_key", lambda key: _FakeProvider2())

    update = FakeUpdate()
    await bot._save_discovered_keys(update, [GROQ_KEY])

    assert "GROQ_API_KEY=existing" in target.read_text(encoding="utf-8")
    assert GROQ_KEY not in target.read_text(encoding="utf-8")
    assert any("Tidak aku simpan" in s or "tidak" in s.lower() for s in update.effective_message.sent)


def _async(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()
