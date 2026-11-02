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
    deepseek_api_key: str = ""
    cerebras_api_key: str = ""
    mistral_api_key: str = ""
    custom_llm_base_url: str = ""
    custom_llm_api_key: str = ""

    llm_fallback_order: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "groq",
            "gemini",
            "cerebras",
            "deepseek",
            "openrouter",
            "mistral",
            "hf",
            "openai",
            "anthropic",
        ]
    )
    small_model: str = "groq/openai/gpt-oss-20b"
    big_model: str = "groq/openai/gpt-oss-120b"
    # Daily token ceiling. It used to default to 0 (unlimited), which is
    # exactly how one day reached a million tokens: nothing ever said "enough".
    # 150k is roughly a few hundred normal chats or a handful of web builds.
    # Set 0 only when you deliberately want no ceiling.
    daily_token_budget: int = 150000
    semantic_cache_enabled: bool = True
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2

    # ---------- Disk retention ----------
    # A 24/7 bot generates images and video forever, and a full disk stops it
    # dead. Old generated files are deleted past these limits; saved logins, the
    # memory database and the newest files are never touched.
    outputs_max_mb: int = 2048
    outputs_max_age_days: int = 14
    retention_enabled: bool = True
    retention_interval_hours: int = 6
    # A silent restart is indistinguishable from a bot that is down, so the bot
    # says hello once per start.
    notify_on_start: bool = True

    # ---------- Sandbox (agent menguji kodenya sendiri) ----------
    # Kode yang dibuat agent dijalankan di sini dulu. Kalau Docker ada, kode
    # berjalan di container terpisah tanpa akses ke .env bot; kalau tidak, ia
    # jalan sebagai subprocess dengan environment yang sudah dibersihkan.
    sandbox_timeout_seconds: int = 60
    sandbox_allow_network: bool = False

    # ---------- Browser agent ----------
    # How many observe/act steps the agent may take on one page before it stops.
    browser_max_steps: int = 8
    browser_login_seconds: int = 240
    # How long a task waits for the user to send a verification code before it
    # gives up. Too short and a real code arrives late; too long and a task
    # nobody is watching holds the worker.
    handoff_timeout_seconds: int = 300
    # Public URL of the noVNC page, so /live can hand the user a link.
    live_view_host: str = ""
    live_view_port: int = 12001
    # Headless server? /login runs the browser under xvfb and sends screenshots.
    browser_headful: bool = True
    # Drive the real browser-use agent (github.com/browser-use/browser-use) for
    # tasks that need judgement - multi-page flows, reading a result out of a
    # page. It lives in its own virtualenv because its pins conflict with ours.
    # Empty means "use ./.venv-browser".
    browser_use_venv: str = ""
    # Model for browser-use. Empty lets the driver pick per configured provider.
    browser_use_model: str = ""
    # Headless is required on a server; a headless browser cannot be watched.
    browser_headless: bool = True
    # Steps cap for one browser-use task. Every step is a model call, so this is
    # also a spend cap.
    browser_use_max_steps: int = 8
    # Post the agent's screen into the chat as a moving tile while it works.
    live_feed_enabled: bool = True
    # Seconds between feed updates. Under ~4s the GIFs overlap on a slow link.
    live_feed_interval: float = 6.0

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
    # Server ini punya port publik. Dipakai sebagai hosting cadangan ketika
    # token Cloudflare/GitHub tidak boleh deploy - supaya user selalu dapat
    # LINK, bukan cuma file zip. Isi dengan URL publik server ini.
    public_base_url: str = ""
    site_host_port: int = 12000
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

    @property
    def env_file(self) -> Path:
        """Where keys added at runtime get written. Never committed."""
        return Path(ENV_FILE)

    def provider_keys(self) -> dict[str, str]:
        return {
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
            "hf": self.hf_token,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "deepseek": self.deepseek_api_key,
            "cerebras": self.cerebras_api_key,
            "mistral": self.mistral_api_key,
            "custom": self.custom_llm_api_key,
        }

    def provider_key_pool(self) -> dict[str, list[str]]:
        """Every key per provider, in order.

        A provider can be configured with several keys so one running out of
        free quota does not silence the bot. The plain var counts as slot 1 and
        numbered vars extend it:

            GEMINI_API_KEY=...        -> slot 1
            GEMINI_API_KEY_1=...      -> slot 1 (same as above)
            GEMINI_API_KEY_2=...      -> slot 2
        """
        import os

        env_names = {
            "groq": "GROQ_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "hf": "HF_TOKEN",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "cerebras": "CEREBRAS_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "custom": "CUSTOM_LLM_API_KEY",
        }
        base = self.provider_keys()
        pool: dict[str, list[str]] = {}
        for provider, env_name in env_names.items():
            keys: list[str] = []
            # Prefer os.environ over the frozen field: a key added at runtime
            # lands there, and the bot must pick it up without a restart.
            primary = os.environ.get(env_name, "").strip() or base.get(provider, "")
            for candidate in [primary] + [
                os.environ.get(f"{env_name}_{i}", "") for i in range(1, 11)
            ]:
                candidate = (candidate or "").strip()
                if candidate and candidate not in keys:
                    keys.append(candidate)
            if keys:
                pool[provider] = keys
        return pool

    def available_providers(self) -> list[str]:
        # Read the pool rather than the frozen fields: a key added at runtime
        # (via /keys) lands in os.environ, and the bot must use it without a
        # restart.
        keys = self.provider_key_pool()
        order = [p for p in self.llm_fallback_order if keys.get(p)]
        # A provider with a key but no mention in LLM_FALLBACK_ORDER would be
        # silently ignored - the user adds a DeepSeek key and nothing changes.
        # Append those after the configured order so new keys always take part.
        for provider in keys:
            if provider not in order and provider != "custom":
                order.append(provider)
        if self.custom_llm_base_url and self.custom_llm_api_key and "custom" not in order:
            order.append("custom")
        return order

    def secret_values(self) -> list[str]:
        """All non-empty secret-ish values, used by the redaction layer."""
        values = [v for v in self.provider_keys().values() if v and len(v) >= 8]
        # Numbered pool keys never reach provider_keys(), so add them here or
        # they would slip through redaction and land in a log.
        for keys in self.provider_key_pool().values():
            values += [k for k in keys if k and len(k) >= 8]
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
            self.deepseek_api_key,
            self.cerebras_api_key,
            self.mistral_api_key,
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
