"""Secret redaction.

Every string that can reach a log, a Telegram message, or a screenshot passes
through here first. Two layers:

1. Value-based: exact secret values loaded from settings get replaced.
2. Pattern-based: things that *look* like secrets (bot tokens, API keys, JWTs,
   bearer headers, private keys) get replaced even if we never loaded them.
"""
from __future__ import annotations

import re
from typing import Iterable

PLACEHOLDER = "***REDACTED***"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token: 123456789:AA...
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,40}\b"), PLACEHOLDER),
    # OpenAI / Groq / OpenRouter style
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), PLACEHOLDER),
    (re.compile(r"\bgsk_[A-Za-z0-9_-]{16,}\b"), PLACEHOLDER),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{16,}\b"), PLACEHOLDER),
    # Google API keys
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), PLACEHOLDER),
    # Hugging Face
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), PLACEHOLDER),
    # GitHub tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), PLACEHOLDER),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), PLACEHOLDER),
    # Slack / generic bearer
    (re.compile(r"(?i)\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"), PLACEHOLDER),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)(\S+)"), r"\1" + PLACEHOLDER),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), PLACEHOLDER),
    # Private key blocks
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        PLACEHOLDER,
    ),
    # key=value style secrets in logs
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)\b(\s*[=:]\s*)([^\s\"',;]{6,})"
        ),
        r"\1\2" + PLACEHOLDER,
    ),
]

# Keys whose *values* must never be rendered, regardless of pattern match.
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "bot_token",
    "access_token",
    "refresh_token",
    "client_secret",
    "secret",
    "password",
    "service_role_key",
    "anon_key",
    "authorization",
    "cookie",
    "set-cookie",
}


def _values() -> Iterable[str]:
    try:
        from app.core.config import get_settings

        return get_settings().secret_values()
    except Exception:  # settings may be unavailable in tiny scripts/tests
        return []


def redact(text: str) -> str:
    """Return *text* with secrets and secret-looking substrings masked."""
    if not text:
        return text
    out = str(text)
    for value in _values():
        if value and value in out:
            out = out.replace(value, PLACEHOLDER)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_mapping(data: dict) -> dict:
    """Redact a mapping, blanking sensitive keys entirely."""
    result: dict = {}
    for key, value in data.items():
        if str(key).lower().replace("-", "_") in SENSITIVE_KEYS:
            result[key] = PLACEHOLDER
        elif isinstance(value, dict):
            result[key] = redact_mapping(value)
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    return result


def assert_no_secrets(text: str, context: str = "text") -> str:
    """Safety net used before publishing text/screenshots to a chat.

    Raises ``SecretLeakError`` when a known secret value is still present.
    """
    from app.core.errors import SecretLeakError

    for value in _values():
        if value and value in text:
            raise SecretLeakError(f"Refusing to publish {context}: known secret value detected")
    return text
