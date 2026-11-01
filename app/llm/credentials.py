"""Add a provider key at runtime, without a restart or a code edit.

The user pastes a key into Telegram, the bot checks it against the provider,
appends it to .env as the next free slot, and starts using it immediately. The
key is written to .env only - never logged, never echoed back in full.

Login is the one step the bot cannot do: creating accounts and clearing captchas
is against every provider's terms and would get the user's accounts banned. So
``LOGIN_URLS`` exists to hand the user the exact page to open, after which the
bot does the rest.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Provider:
    name: str
    env_var: str
    label: str
    signup_url: str
    console_url: str
    free_note: str
    litellm_prefix: str


# Ordered by how generous the free tier is, so the list doubles as advice.
PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        name="gemini",
        env_var="GEMINI_API_KEY",
        label="Google Gemini",
        signup_url="https://aistudio.google.com/apikey",
        console_url="https://aistudio.google.com/apikey",
        free_note="~1500 request/hari, paling royal",
        litellm_prefix="gemini/",
    ),
    "cerebras": Provider(
        name="cerebras",
        env_var="CEREBRAS_API_KEY",
        label="Cerebras",
        signup_url="https://cloud.cerebras.ai",
        console_url="https://cloud.cerebras.ai/platform",
        free_note="~1 juta token/hari, cepat",
        litellm_prefix="cerebras/",
    ),
    "groq": Provider(
        name="groq",
        env_var="GROQ_API_KEY",
        label="Groq",
        signup_url="https://console.groq.com/keys",
        console_url="https://console.groq.com/keys",
        free_note="cepat, limit harian ketat",
        litellm_prefix="groq/",
    ),
    "deepseek": Provider(
        name="deepseek",
        env_var="DEEPSEEK_API_KEY",
        label="DeepSeek",
        signup_url="https://platform.deepseek.com/sign_up",
        console_url="https://platform.deepseek.com/api_keys",
        free_note="murah, reasoning kuat",
        litellm_prefix="deepseek/",
    ),
    "openrouter": Provider(
        name="openrouter",
        env_var="OPENROUTER_API_KEY",
        label="OpenRouter",
        signup_url="https://openrouter.ai/keys",
        console_url="https://openrouter.ai/keys",
        free_note="banyak model gratis (:free)",
        litellm_prefix="openrouter/",
    ),
    "mistral": Provider(
        name="mistral",
        env_var="MISTRAL_API_KEY",
        label="Mistral",
        signup_url="https://console.mistral.ai",
        console_url="https://console.mistral.ai/api-keys",
        free_note="ada tier gratis",
        litellm_prefix="mistral/",
    ),
    "hf": Provider(
        name="hf",
        env_var="HF_TOKEN",
        label="HuggingFace",
        signup_url="https://huggingface.co/join",
        console_url="https://huggingface.co/settings/tokens",
        free_note="gratis, juga untuk gambar FLUX",
        litellm_prefix="huggingface/",
    ),
}

_ALIASES = {
    "google": "gemini",
    "googleai": "gemini",
    "google-ai": "gemini",
    "ai studio": "gemini",
    "google ai": "gemini",
    "google gemini": "gemini",
    "aistudio": "gemini",
    "huggingface": "hf",
    "hugging face": "hf",
    "open router": "openrouter",
    "deep seek": "deepseek",
    "cerebras ai": "cerebras",
}

# Keys have a recognisable shape; reject obvious junk before writing it to .env.
_KEY_PATTERNS = {
    "groq": r"^gsk_[A-Za-z0-9]{20,}$",
    "gemini": r"^AIza[A-Za-z0-9_\-]{30,}$",
    "openrouter": r"^sk-or-v1-[A-Za-z0-9]{20,}$",
    "deepseek": r"^sk-[A-Za-z0-9]{20,}$",
    "hf": r"^hf_[A-Za-z0-9]{20,}$",
    "cerebras": r"^csk-[A-Za-z0-9]{20,}$",
}


def resolve_provider(name: str) -> Provider | None:
    """Accept 'gemini', 'Gemini', 'google', 'google ai', ..."""
    if not name:
        return None
    clean = name.strip().lower()
    clean = _ALIASES.get(clean, clean)
    if clean in PROVIDERS:
        return PROVIDERS[clean]
    for key, provider in PROVIDERS.items():
        if clean in (key, provider.label.lower()):
            return provider
    return None


def find_key_in_text(text: str) -> tuple[Provider, str] | None:
    """Pull a provider and key out of free-form text like 'gemini AIza...'."""
    if not text:
        return None
    provider: Provider | None = None
    lowered = text.lower()
    for alias, name in _ALIASES.items():
        if alias in lowered:
            provider = PROVIDERS[name]
            break
    if provider is None:
        for name, candidate in PROVIDERS.items():
            if name in lowered or candidate.label.lower() in lowered:
                provider = candidate
                break

    token = ""
    for chunk in text.split():
        candidate = chunk.strip().strip("`'\".,")
        if len(candidate) >= 20 and re.fullmatch(r"[A-Za-z0-9_\-]{20,}", candidate):
            token = candidate
            break
    if not token:
        return None

    if provider is None:
        provider = guess_provider_from_key(token)
    if provider is None:
        return None
    return provider, token


def guess_provider_from_key(key: str) -> Provider | None:
    for name, pattern in _KEY_PATTERNS.items():
        if re.match(pattern, key):
            return PROVIDERS[name]
    return None


def key_shape_ok(provider: Provider, key: str) -> bool:
    pattern = _KEY_PATTERNS.get(provider.name)
    if not pattern:
        return len(key) >= 20
    return bool(re.match(pattern, key))


def next_slot(env_path: Path, env_var: str) -> int:
    """First free numbered slot for a provider, e.g. GEMINI_API_KEY_2."""
    existing = {1} if os.environ.get(env_var, "").strip() else set()
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        for i in range(1, 11):
            if re.search(rf"^{env_var}_{i}\s*=\s*\S", content, re.MULTILINE):
                existing.add(i)
    slot = 1
    while slot in existing:
        slot += 1
    return slot


def append_key(env_path: Path, provider: Provider, key: str, slot: int) -> str:
    """Write the key as the given slot. Returns the variable name used."""
    var = provider.env_var if slot == 1 else f"{provider.env_var}_{slot}"
    line = f"{var}={key}\n"
    if not env_path.exists():
        env_path.write_text(
            "# Dibuat otomatis oleh /keys - jangan di-commit.\n" + line, encoding="utf-8"
        )
    else:
        content = env_path.read_text(encoding="utf-8")
        if not content.endswith("\n"):
            content += "\n"
        content += line
        env_path.write_text(content, encoding="utf-8")
    # Make it usable right now, not after a restart.
    os.environ[var] = key
    if slot == 1:
        os.environ[provider.env_var] = key
    log.info("key pool: added %s slot %s", provider.name, slot)
    return var


def mask(key: str) -> str:
    if len(key) <= 4:
        return "****"
    return f"****{key[-4:]}"


def login_help(provider: Provider | None = None) -> str:
    """What the user must do by hand: open the page and sign in."""
    if provider is None:
        lines = ["🔐 *Cara nambah key*", ""]
        for p in PROVIDERS.values():
            lines.append(f"*{p.label}* — {p.free_note}\n{p.console_url}")
        lines += [
            "",
            "Login tetap kamu yang lakukan. Aku tidak bisa (dan tidak boleh) "
            "bikin akun atau isi captcha otomatis — itu bikin akun kamu kena ban.",
            "",
            "Setelah dapat key, kirim ke sini seperti ini:",
            "`/keys add gemini AIza...`",
        ]
        return "\n".join(lines)
    return (
        f"🔐 *{provider.label}*\n\n"
        f"1. Buka: {provider.signup_url}\n"
        f"2. Login / daftar (langkah ini tangan kamu)\n"
        f"3. Buka: {provider.console_url}\n"
        f"4. Copy API key-nya\n"
        f"5. Kirim ke sini: `/keys add {provider.name} <key>`\n\n"
        f"Gratis: {provider.free_note}\n"
        "Key langsung dipakai tanpa restart. Aku simpan di `.env`, "
        "tidak pernah aku tampilkan lagi."
    )
