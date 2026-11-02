"""LiteLLM gateway: one call site, many providers, automatic fallback.

Design rules:
* Callers ask for a *tier* ("small" | "big"), never a provider model string.
  The router expands the tier into a provider-ordered candidate list.
* A candidate is skipped when its API key is missing, so a partially configured
  ``.env`` still works.
* Failures (rate limit, 5xx, timeout, content filter) move to the next
  candidate. Only when every candidate fails do we raise.
* Token usage is recorded per user per day, and every prompt is trimmed to the
  tier's budget before it leaves the process.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import InvalidApiKeyError, MissingSecretError, ProviderUnavailableError
from app.core.logging_setup import get_logger
from app.core.utils import run_blocking
from app.llm.key_pool import looks_like_rate_limit
from app.llm.tokens import count_message_tokens, trim_messages

log = get_logger(__name__)

# The task currently running, so every call made inside it is charged to the
# same ceiling. A ContextVar keeps this correct per task instead of per process,
# which matters because the bot handles users concurrently.
_CURRENT_TASK: ContextVar[Any] = ContextVar("llm_current_task", default=None)


@dataclass
class _TaskBinding:
    """A context manager that scopes LLM spending to one task."""

    gateway: Any
    task: Any
    _token: Any = None

    def __enter__(self):
        self._token = _CURRENT_TASK.set(self.task)
        return self.task

    def __exit__(self, *exc):
        _CURRENT_TASK.reset(self._token)
        return False


def _TASK_BINDING(gateway, task):
    return _TaskBinding(gateway=gateway, task=task)

# Logical tier -> per-provider model, tried in the user's configured order.
TIER_MODELS: dict[str, dict[str, str]] = {
    "small": {
        "groq": "groq/openai/gpt-oss-20b",
        "gemini": "gemini/gemini-2.0-flash-lite",
        "openrouter": "openrouter/meta-llama/llama-3.1-8b-instruct",
        "hf": "huggingface/meta-llama/Meta-Llama-3-8B-Instruct",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-20241022",
        "deepseek": "deepseek/deepseek-chat",
        "cerebras": "cerebras/llama3.1-8b",
        "mistral": "mistral/mistral-small-latest",
        "custom": "openai/local-small",
    },
    "big": {
        "groq": "groq/openai/gpt-oss-120b",
        "gemini": "gemini/gemini-2.0-flash",
        "openrouter": "openrouter/meta-llama/llama-3.3-70b-instruct",
        "hf": "huggingface/meta-llama/Meta-Llama-3-70B-Instruct",
        "openai": "gpt-4o",
        "anthropic": "claude-3-5-sonnet-20241022",
        "deepseek": "deepseek/deepseek-reasoner",
        "cerebras": "cerebras/llama-3.3-70b",
        "mistral": "mistral/mistral-large-latest",
        "custom": "openai/local-big",
    },
}

# Prompt budget per tier (input side). Output is capped separately.
TIER_PROMPT_BUDGET = {"small": 6000, "big": 24000}
TIER_MAX_OUTPUT = {"small": 1200, "big": 4000}

# Reasoning models spend output tokens on hidden thinking before they emit any
# text. Give them a small cap and they think until the budget is gone and return
# an empty string - which looks like a broken model but is really a starved one.
REASONING_MODEL_MARKERS = (
    "gpt-oss",
    "o1",
    "o3",
    "o4",
    "deepseek-reasoner",
    "qwen3",
    "thinking",
)
REASONING_MIN_OUTPUT = 900


def _needs_reasoning_floor(model: str) -> bool:
    lowered = model.lower()
    return any(marker in lowered for marker in REASONING_MODEL_MARKERS)


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: list[str] = field(default_factory=list)
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _provider_env(provider: str, settings) -> tuple[str, str | None]:
    """Return (api_key, api_base) for a provider."""
    mapping = {
        "groq": (settings.groq_api_key, None),
        "gemini": (settings.gemini_api_key, None),
        "openrouter": (settings.openrouter_api_key, None),
        "hf": (settings.hf_token, None),
        "openai": (settings.openai_api_key, None),
        "anthropic": (settings.anthropic_api_key, None),
        "custom": (settings.custom_llm_api_key, settings.custom_llm_base_url or None),
    }
    return mapping.get(provider, ("", None))


def _export_provider_env(provider: str, settings) -> None:
    """LiteLLM reads provider keys from os.environ; set them at call time only."""
    key, base = _provider_env(provider, settings)
    if not key:
        return
    names = {
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
    env_name = names.get(provider)
    if env_name:
        os.environ.setdefault(env_name, key)
    if provider == "hf":
        os.environ.setdefault("HUGGINGFACE_API_KEY", key)
    if base:
        os.environ.setdefault("CUSTOM_LLM_API_BASE", base)


class LLMGateway:
    def __init__(self, settings, budget=None, key_pool=None, cost_guard=None):
        self.settings = settings
        self.budget = budget
        self._litellm = None
        self.key_pool = key_pool
        self.cost_guard = cost_guard

    # ------------------------------------------------------------------
    def _import(self):
        if self._litellm is None:
            try:
                from litellm import acompletion  # type: ignore
            except Exception as exc:  # pragma: no cover
                raise ProviderUnavailableError(
                    "LiteLLM tidak terpasang. Jalankan: pip install litellm", detail=str(exc)
                ) from exc
            self._litellm = acompletion
        return self._litellm

    def available(self) -> bool:
        return bool(self.settings.available_providers())

    # ------------------------------------------------------------ task scope
    def _current_task(self):
        return _CURRENT_TASK.get()

    def bind_task(self, task):
        """Charge every call inside this block to ``task``.

        Returns the context manager itself: ``_TASK_BINDING`` builds one, it is
        not a ContextVar, so calling ``.set`` on it crashed every chat with
        "'function' object has no attribute 'set'".
        """
        return _TASK_BINDING(self, task)

    def candidates(self, tier: str) -> list[tuple[str, str]]:
        models = TIER_MODELS.get(tier, TIER_MODELS["small"])
        out: list[tuple[str, str]] = []
        available = self.settings.available_providers()

        # A SMALL_MODEL/BIG_MODEL override wins, but only when its provider has a
        # key configured - otherwise the fallback chain would start with a model
        # that can never be called.
        preferred = self.settings.small_model if tier == "small" else self.settings.big_model
        if preferred and "/" in preferred:
            provider = preferred.split("/", 1)[0]
            if provider in available:
                out.append((provider, preferred))

        for provider in available:
            model = models.get(provider)
            if model and (provider, model) not in out:
                out.append((provider, model))
        return out

    def _apply_pool_key(self, provider: str) -> None:
        """Point the provider's env var at the pool's currently active key."""
        if not self.key_pool:
            _export_provider_env(provider, self.settings)
            return
        state = self.key_pool.active(provider)
        if state is None:
            _export_provider_env(provider, self.settings)
            return
        names = {
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
        env_name = names.get(provider)
        if env_name:
            os.environ[env_name] = state.key
        if provider == "hf":
            os.environ["HUGGINGFACE_API_KEY"] = state.key

    def require_ready(self) -> None:
        if not self.available():
            raise MissingSecretError(
                "GROQ_API_KEY (atau GEMINI_API_KEY / OPENROUTER_API_KEY / HF_TOKEN)",
                detail="Tidak ada provider LLM yang terkonfigurasi.",
            )

    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: list[dict],
        *,
        tier: str = "small",
        user_id: int = 0,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        json_mode: bool = False,
    ) -> LLMResult:
        self.require_ready()
        acompletion = self._import()

        budget_tokens = TIER_PROMPT_BUDGET.get(tier, 6000)
        prepared = trim_messages(messages, budget_tokens)
        prompt_tokens = count_message_tokens(prepared)
        output_cap = max_tokens or TIER_MAX_OUTPUT.get(tier, 1200)
        attempts: list[str] = []
        last_error: Exception | None = None

        # Refuse before spending, not after. The day total comes from the store
        # so a restart cannot reset the meter, and the per-task ceiling stops a
        # repair loop from eating the whole day on one stubborn site.
        task = self._current_task()
        if self.cost_guard is not None:
            day_used = 0
            if self.budget is not None and user_id:
                try:
                    day_used = (await self.budget.usage(user_id))["total_tokens"]
                except Exception as exc:  # noqa: BLE001 - never block on bookkeeping
                    log.warning("usage lookup failed: %s", exc)
            self.cost_guard.check(
                wanted=prompt_tokens + output_cap, day_used=day_used, task=task
            )

        for provider, model in self.candidates(tier):
            # Pick the active key from the pool and force it into the env var
            # LiteLLM reads. ``setdefault`` would keep a benched key alive after
            # the first call, so overwrite explicitly.
            self._apply_pool_key(provider)
            model_cap = output_cap
            if _needs_reasoning_floor(model):
                model_cap = max(output_cap, REASONING_MIN_OUTPUT)
            for attempt in range(self.settings.llm_max_retries + 1):
                started = time.monotonic()
                try:
                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": prepared,
                        "temperature": temperature,
                        "max_tokens": model_cap,
                        "timeout": self.settings.llm_timeout_seconds,
                    }
                    if json_mode:
                        kwargs["response_format"] = response_format or {"type": "json_object"}
                    response = await asyncio.wait_for(
                        acompletion(**kwargs),
                        timeout=self.settings.llm_timeout_seconds + 10,
                    )
                    text = self._extract_text(response)
                    if not text.strip():
                        # A reasoning model that ran out of output budget returns
                        # empty text. Retrying with more room beats handing the
                        # user a blank message.
                        raise RuntimeError(
                            "model returned empty text (output budget exhausted?)"
                        )
                    usage = getattr(response, "usage", None)
                    p_tokens = int(getattr(usage, "prompt_tokens", 0) or prompt_tokens)
                    c_tokens = int(
                        getattr(usage, "completion_tokens", 0) or max(1, len(text) // 4)
                    )
                    if self.budget and user_id:
                        await self.budget.record(user_id, model, p_tokens, c_tokens)
                    if task is not None:
                        task.record(p_tokens + c_tokens, note=f"{provider}/{tier}")
                    if self.key_pool:
                        self.key_pool.record_use(provider)
                    # "module" is a reserved LogRecord attribute; passing it in
                    # extra raises KeyError and, inside this try block, made a
                    # successful call look like a failed provider.
                    log.info(
                        "llm ok",
                        extra={
                            "user_id": user_id,
                            "component": "llm",
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "status": "ok",
                        },
                    )
                    return LLMResult(
                        text=text,
                        model=model,
                        provider=provider,
                        prompt_tokens=p_tokens,
                        completion_tokens=c_tokens,
                        attempts=attempts + [f"{provider}:ok"],
                    )
                except Exception as exc:  # noqa: BLE001 - fallback is the point
                    last_error = exc
                    attempts.append(f"{provider}:fail({type(exc).__name__})")
                    log.warning(
                        "llm attempt failed provider=%s model=%s attempt=%s err=%s",
                        provider,
                        model,
                        attempt,
                        exc,
                    )
                    # "Request too large" is a token limit on the prompt, so
                    # rotating keys cannot help - the request itself must shrink.
                    # This must run before, and independently of, the key pool: a
                    # single-key setup hits the same wall.
                    #
                    # The provider counts input *and* the max_tokens reservation
                    # against its per-minute budget, so both have to come down.
                    # One aggressive step is deliberate: the retry budget is
                    # small, and halving 24000 to 12000 would still exceed an
                    # 8000/min free tier and burn the remaining attempts.
                    if _looks_like_prompt_too_large(exc) and budget_tokens > 2000:
                        model_cap = min(model_cap, 2000)
                        budget_tokens = min(budget_tokens // 2, 5000 - model_cap)
                        budget_tokens = max(1500, budget_tokens)
                        prepared = trim_messages(messages, budget_tokens)
                        prompt_tokens = count_message_tokens(prepared)
                        attempts.append(f"{provider}:shrink->{budget_tokens}")
                        await asyncio.sleep(min(3.0, 1.0 * (attempt + 1)))
                        continue
                    # A throttled key is not a broken provider. Bench it, swap
                    # in the next one, and retry here before falling through -
                    # otherwise one exhausted free tier takes the whole
                    # provider out even though a healthy key sits idle.
                    if self.key_pool and looks_like_rate_limit(exc):
                        next_state = self.key_pool.rotate(provider, exc)
                        if next_state is not None and self.key_pool.slots(provider) > 1:
                            attempts.append(f"{provider}:rotate->{next_state.label()}")
                            self._apply_pool_key(provider)
                            continue
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(min(2.0, 0.5 * (2**attempt)))
                    else:
                        break  # move to next provider

        summary = "Semua provider gagal: " + ", ".join(attempts)
        # LiteLLM reports an invalid key as BadRequestError, which reads like a
        # malformed request and sent us chasing the wrong problem. Say plainly
        # that the key was rejected.
        if _looks_like_auth_failure(last_error):
            raise InvalidApiKeyError(summary, detail=str(last_error))
        raise ProviderUnavailableError(summary, detail=str(last_error))

    # ------------------------------------------------------------------
    async def complete_json(
        self, messages: list[dict], *, tier: str = "small", user_id: int = 0, **kwargs
    ) -> dict:
        """Call the model and parse a JSON object out of the answer."""
        import json
        import re

        result = await self.complete(
            messages, tier=tier, user_id=user_id, json_mode=True, **kwargs
        )
        text = result.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        log.warning("complete_json: model returned non-JSON, wrapping as {text:...}")
        return {"text": result.text, "_parse_error": True}

    async def stream_text(self, messages: list[dict], *, tier: str = "small", user_id: int = 0):
        """Async generator of text chunks, with the same fallback semantics."""
        self.require_ready()
        acompletion = self._import()
        budget_tokens = TIER_PROMPT_BUDGET.get(tier, 6000)
        prepared = trim_messages(messages, budget_tokens)

        last_error: Exception | None = None
        for provider, model in self.candidates(tier):
            _export_provider_env(provider, self.settings)
            try:
                stream = await acompletion(
                    model=model,
                    messages=prepared,
                    temperature=0.4,
                    max_tokens=TIER_MAX_OUTPUT.get(tier, 1200),
                    timeout=self.settings.llm_timeout_seconds,
                    stream=True,
                )
                async for chunk in stream:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        yield content
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("stream failed provider=%s err=%s", provider, exc)
        raise ProviderUnavailableError("Stream gagal di semua provider", detail=str(last_error))

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_text(response) -> str:
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderUnavailableError("Respons LLM tidak dikenali", detail=str(exc)) from exc
        if content is None:
            return ""
        if isinstance(content, list):  # some providers return content parts
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text", "")))
                else:
                    parts.append(str(part))
            return "".join(parts)
        return str(content)


def build_gateway(settings, budget=None, key_pool=None) -> LLMGateway:
    return LLMGateway(settings, budget=budget, key_pool=key_pool)


_AUTH_FAILURE_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "api key not valid",
    "authentication",
    "unauthorized",
    "permission denied",
    "invalid authentication",
)


def _looks_like_prompt_too_large(error: Exception | None) -> bool:
    """True when the provider rejected the request because it is too big.

    Distinct from throttling: rotating to another key does not help, because the
    limit is tokens-per-minute on the *prompt*, not requests on the key. The only
    fix is to send less. Groq's free tier, for example, allows 8000 tokens per
    minute, and a web-build prompt of 11k was rejected every single time while
    the bot dutifully benched and rotated a perfectly healthy key.
    """
    if error is None:
        return False
    blob = f"{type(error).__name__} {error}".lower()
    return any(
        marker in blob
        for marker in (
            "request too large",
            "tokens per minute",
            "tpm",
            "reduce your message size",
            "context length",
            "context_length_exceeded",
            "maximum context",
            "too many tokens",
        )
    )


def _looks_like_auth_failure(error: Exception | None) -> bool:
    """True when the provider rejected the credential rather than the request."""
    if error is None:
        return False
    if isinstance(error, PermissionError):
        return True
    blob = f"{type(error).__name__} {error}".lower()
    return any(marker in blob for marker in _AUTH_FAILURE_MARKERS)
