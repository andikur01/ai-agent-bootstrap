"""Central configuration. All secrets come from env/.env only - never hardcoded."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]

# Tests point this at a non-existent file so a developer's real .env can never
# leak into a test run (and flip assertions that assume no provider is set).
ENV_FILE = os.getenv("ENV_FILE", str(ROOT_DIR / ".env"))


def _split_csv(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Telegram ----------
    telegram_bot_token: str = ""
    # Point at a local Bot API server (or a test double) instead of api.telegram.org.
    telegram_api_base: str = ""
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_admin_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # ---------- LLM ----------
    groq_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    hf_token: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    custom_llm_base_url: str = ""
    custom_llm_api_key: str = ""

    llm_fallback_order: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["groq", "gemini", "openrouter", "hf", "openai"]
    )
    small_model: str = "groq/llama-3.1-8b-instant"
    big_model: str = "groq/llama-3.3-70b-versatile"
    daily_token_budget: int = 200_000
    semantic_cache_enabled: bool = True
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2

    # ---------- Search ----------
    tavily_api_key: str = ""
    searxng_base_url: str = ""

    # ---------- Memory ----------
    chroma_host: str = ""
    chroma_port: int = 8000
    memory_dir: str = "./data/memory"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384
    memory_top_k: int = 6
    memory_max_context_tokens: int = 1200

    # ---------- Media ----------
    pollinations_base_url: str = "https://image.pollinations.ai/prompt"
    replicate_api_token: str = ""
    manim_quality: str = "medium_quality"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # ---------- Deploy ----------
    github_token: str = ""
    github_owner: str = ""
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""
    render_api_key: str = ""
    render_owner_id: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_db_password: str = ""

    # ---------- n8n ----------
    n8n_base_url: str = "http://localhost:5678"
    n8n_api_key: str = ""

    # ---------- YouTube ----------
    youtube_client_secret_file: str = ""
    youtube_token_file: str = "./data/youtube_token.json"
    ytdlp_cookies_file: str = ""

    # ---------- Runtime ----------
    log_level: str = "INFO"
    log_json: bool = True
    tz: str = "Asia/Jakarta"
    outputs_dir: str = "./outputs"
    data_dir: str = "./data"
    max_upload_mb: int = 45
    rate_limit_per_minute: int = 20
    heavy_task_daily_limit: int = 25

    @field_validator(
        "telegram_allowed_user_ids", "telegram_admin_user_ids", "llm_fallback_order", mode="before"
    )
    @classmethod
    def _parse_lists(cls, value: object) -> list:
        return _split_csv(value)

    @property
    def outputs_path(self) -> Path:
        path = Path(self.outputs_dir)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def data_path(self) -> Path:
        path = Path(self.data_dir)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def memory_path(self) -> Path:
        path = Path(self.memory_dir)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sqlite_path(self) -> Path:
        return self.data_path / "agent.sqlite3"

    def provider_keys(self) -> dict[str, str]:
        return {
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
            "hf": self.hf_token,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "custom": self.custom_llm_api_key,
        }

    def available_providers(self) -> list[str]:
        keys = self.provider_keys()
        order = [p for p in self.llm_fallback_order if keys.get(p)]
        if self.custom_llm_base_url and self.custom_llm_api_key and "custom" not in order:
            order.append("custom")
        return order

    def secret_values(self) -> list[str]:
        """All non-empty secret-ish values, used by the redaction layer."""
        values = [v for v in self.provider_keys().values() if v and len(v) >= 8]
        for extra in (
            self.telegram_bot_token,
            self.tavily_api_key,
            self.github_token,
            self.cloudflare_api_token,
            self.render_api_key,
            self.supabase_service_role_key,
            self.supabase_anon_key,
            self.supabase_db_password,
            self.n8n_api_key,
            self.replicate_api_token,
        ):
            if extra and len(extra) >= 8:
                values.append(extra)
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
