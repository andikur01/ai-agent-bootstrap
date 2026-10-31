#!/usr/bin/env python3
"""Preflight check: what is configured, what is missing, what works.

    python scripts/doctor.py

Prints a table of every integration and whether it is ready. Never prints a
secret value - only whether a key is present.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK = "✅"
WARN = "⚠️ "
MISS = "❌"


def line(label: str, status: str, detail: str = "") -> None:
    print(f"{status} {label:<28} {detail}")


def main() -> int:
    from app.core.config import get_settings

    settings = get_settings()
    print("\n=== Konfigurasi ===\n")

    line("Telegram bot token", OK if settings.telegram_bot_token else MISS,
         "" if settings.telegram_bot_token else "isi TELEGRAM_BOT_TOKEN")
    allowed = settings.telegram_allowed_user_ids
    line("Allow-list user", OK if allowed else MISS,
         f"{len(allowed)} user" if allowed else "isi TELEGRAM_ALLOWED_USER_IDS (wajib)")

    providers = settings.available_providers()
    line("LLM provider", OK if providers else MISS,
         ", ".join(providers) if providers else "isi minimal satu API key")

    print("\n=== Opsional (fitur tertentu) ===\n")
    line("Tavily (riset bagus)", OK if settings.tavily_api_key else WARN,
         "fallback DuckDuckGo aktif" if not settings.tavily_api_key else "siap")
    line("SearXNG", OK if settings.searxng_base_url else WARN, settings.searxng_base_url or "belum diset")
    line("HuggingFace (FLUX gambar)", OK if settings.hf_token else WARN,
         "fallback Pollinations aktif" if not settings.hf_token else "siap")
    line("Replicate (video AI)", OK if settings.replicate_api_token else WARN, "opsional")
    line("Cloudflare Pages", OK if settings.cloudflare_api_token and settings.cloudflare_account_id else WARN,
         "deploy static langsung" if settings.cloudflare_api_token else "opsional")
    line("GitHub token+owner", OK if settings.github_token and settings.github_owner else WARN,
         "Pages + repo deploy")
    line("Render", OK if settings.render_api_key and settings.render_owner_id else WARN,
         "deploy fullstack")
    line("Supabase", OK if settings.supabase_url and settings.supabase_anon_key else WARN,
         "web app login+database")
    line("n8n API", OK if settings.n8n_api_key else WARN, settings.n8n_base_url)
    line("yt-dlp cookies", OK if settings.ytdlp_cookies_file and Path(settings.ytdlp_cookies_file).exists() else WARN,
         "untuk link yang diblokir")
    token_file = Path(settings.youtube_token_file)
    line("YouTube OAuth token", OK if token_file.exists() else WARN,
         "jalankan scripts/youtube_auth.py" if not token_file.exists() else "siap")

    print("\n=== Tool sistem ===\n")
    import importlib.util
    import sys as _sys

    bin_dir = Path(_sys.executable).parent
    for name in ("ffmpeg", "ffprobe", "manim", "espeak-ng"):
        path = shutil.which(name) or shutil.which(name, path=str(bin_dir))
        if not path and importlib.util.find_spec(name.replace("-", "_")) is not None:
            path = "(modul Python)"
        line(name, OK if path else WARN, path or "tidak terpasang")

    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright
        has_browser = True
    except ImportError:
        has_browser = False
    line("Playwright", OK if has_browser else WARN,
         "QA screenshot website" if has_browser else "pip install playwright")

    print("\n=== Cek runtime ===\n")
    return asyncio.run(_runtime_checks(settings))


async def _runtime_checks(settings) -> int:
    failures = 0

    from app.memory.embeddings import build_embedder
    from app.memory.vector_store import build_vector_store
    from app.db.store import Store

    store = Store(settings.sqlite_path)
    try:
        embedder = build_embedder(settings)
        vectors = build_vector_store(settings, store, embedder)
        line("Vector memory", OK, getattr(vectors, "backend", "?"))
        line("Embedding", OK, f"dim={embedder.dim}")
    except Exception as exc:  # noqa: BLE001
        line("Vector memory", MISS, str(exc)[:80])
        failures += 1

    if settings.available_providers():
        from app.llm.gateway import LLMGateway

        gateway = LLMGateway(settings)
        try:
            result = await gateway.complete(
                [{"role": "user", "content": "Balas dengan satu kata: ok"}],
                tier="small",
                max_tokens=10,
            )
            line("LLM panggilan nyata", OK, f"{result.provider}/{result.model}")
        except Exception as exc:  # noqa: BLE001
            line("LLM panggilan nyata", MISS, str(exc)[:100])
            failures += 1
    else:
        line("LLM panggilan nyata", WARN, "dilewati (tidak ada provider)")

    if settings.telegram_bot_token:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
                )
            data = response.json()
            if data.get("ok"):
                line("Telegram getMe", OK, "@" + data["result"].get("username", ""))
            else:
                line("Telegram getMe", MISS, data.get("description", "")[:80])
                failures += 1
        except Exception as exc:  # noqa: BLE001
            line("Telegram getMe", MISS, str(exc)[:80])
            failures += 1
    store.close()

    print()
    if failures:
        print(f"{MISS} {failures} masalah kritis perlu diperbaiki.\n")
        return 1
    print(f"{OK} Siap jalan. Coba: python main.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
