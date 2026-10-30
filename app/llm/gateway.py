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
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import MissingSecretError, ProviderUnavailableError
from app.core.logging_setup import get_logger
from app.core.utils import run_blocking
from app.llm.tokens import count_message_tokens, trim_messages

log = get_logger(__name__)

# Logical tier -> per-provider model, tried in the user's configured order.
TIER_MODELS: dict[str, dict[str, str]] = {
    "small": {
        "groq": "groq/llama-3.1-8b-instant",
        "gemini": "gemini/gemini-2.0-flash-lite",
        "openrouter": "openrouter/meta-llama/llama-3.1-8b-instruct",
        "hf": "huggingface/meta-llama/Meta-Llama-3-8B-Instruct",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-20241022",
        "custom": "openai/local-small",
    },
    "big": {
        "groq": "groq/llama-3.3-70b-versatile",
        "gemini": "gemini/gemini-2.0-flash",
        "openrouter": "openrouter/meta-llama/llama-3.3-70b-instruct",
        "hf": "huggingface/meta-llama/Meta-Llama-3-70B-Instruct",
        "openai": "gpt-4o",
        "anthropic": "claude-3-5-sonnet-20241022",
        "custom": "openai/local-big",
    },
}

# Prompt budget per tier (input side). Output is capped separately.
TIER_PROMPT_BUDGET = {"small": 6000, "big": 24000}
TIER_MAX_OUTPUT = {"small": 1200, "big": 4000}


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
    def __init__(self, settings, budget=None):
        self.settings = settings
        self.budget = budget
        self._litellm = None

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

    def candidates(self, tier: str) -> list[tuple[str, str]]:
        models = TIER_MODELS.get(tier, TIER_MODELS["small"])
        out: list[tuple[str, str]] = []
        for provider in self.settings.available_providers():
            model = models.get(provider)
            if model:
                out.append((provider, model))
        return out

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

        for provider, model in self.candidates(tier):
            _export_provider_env(provider, self.settings)
            for attempt in range(self.settings.llm_max_retries + 1):
                started = time.monotonic()
                try:
                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": prepared,
                        "temperature": temperature,
                        "max_tokens": output_cap,
                        "timeout": self.settings.llm_timeout_seconds,
                    }
                    if json_mode:
                        kwargs["response_format"] = response_format or {"type": "json_object"}
                    response = await asyncio.wait_for(
                        acompletion(**kwargs),
                        timeout=self.settings.llm_timeout_seconds + 10,
                    )
                    text = self._extract_text(response)
                    usage = getattr(response, "usage", None)
                    p_tokens = int(getattr(usage, "prompt_tokens", 0) or prompt_tokens)
                    c_tokens = int(
                        getattr(usage, "completion_tokens", 0) or max(1, len(text) // 4)
                    )
                    if self.budget and user_id:
                        await self.budget.record(user_id, model, p_tokens, c_tokens)
                    log.info(
                        "llm ok",
                        extra={
                            "user_id": user_id,
                            "module": "llm",
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
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(min(2.0, 0.5 * (2**attempt)))
                    else:
                        break  # move to next provider

        raise ProviderUnavailableError(
            "Semua provider gagal: " + ", ".join(attempts), detail=str(last_error)
        )

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


def build_gateway(settings, budget=None) -> LLMGateway:
    return LLMGateway(settings, budget=budget)
