"""Typed errors so the bot can react correctly instead of dumping tracebacks."""
from __future__ import annotations


class AgentError(Exception):
    """Base class for expected, user-reportable failures."""

    user_message = "Ada error internal. Coba lagi ya."

    def __init__(self, message: str = "", *, detail: str = ""):
        super().__init__(message or self.user_message)
        self.detail = detail


class ConfigError(AgentError):
    user_message = "Konfigurasi belum lengkap."


class MissingSecretError(ConfigError):
    user_message = "Butuh secret yang belum diisi."

    def __init__(self, name: str, *, detail: str = ""):
        super().__init__(f"Missing secret: {name}", detail=detail)
        self.name = name
        self.user_message = (
            f"Aku butuh *{name}* untuk lanjut. Tolong isi di `.env` "
            f"(atau GitHub/HF Secrets), lalu bilang aku sudah siap."
        )


class ProviderUnavailableError(AgentError):
    user_message = "Semua provider AI sedang tidak bisa dihubungi."


class BudgetExceededError(AgentError):
    user_message = "Budget token harian sudah habis. Naikkan `DAILY_TOKEN_BUDGET` kalau perlu."


class RateLimitedError(AgentError):
    user_message = "Terlalu banyak permintaan. Tunggu sebentar ya."


class UnauthorizedUserError(AgentError):
    user_message = "Maaf, kamu belum diizinkan memakai bot ini."


class SecretLeakError(AgentError):
    user_message = "Aku menahan output karena terdeteksi berisi secret."


class TaskCancelledError(AgentError):
    user_message = "Tugas dibatalkan."


class ToolError(AgentError):
    user_message = "Tool-nya gagal jalan."


class MediaQualityError(ToolError):
    user_message = "Hasil media belum memenuhi standar kualitas setelah beberapa kali perbaikan."


class ArtifactNotFoundError(AgentError):
    user_message = "Aku tidak menemukan artifact yang kamu maksud."
