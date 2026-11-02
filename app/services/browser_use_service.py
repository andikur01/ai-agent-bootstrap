"""Run the real ``browser-use`` agent, out of process.

Why a separate process and not an import
----------------------------------------
``browser-use`` pins its own dependency set (pydantic, pillow, openai, mcp,
google-genai, ...). Installing it into the bot's virtualenv downgrades packages
the bot is already running on - pillow 13 to 12, pydantic 2.14 to 2.13 - which is
a live risk to image generation and config parsing. So it lives in its own
virtualenv (``.venv-browser``) and this module talks to it over a subprocess,
exchanging one JSON object on stdout. The bot keeps its own pins; the specialist
keeps its own; neither can break the other.

Why browser-use at all
----------------------
This is the "give it to the expert" call. The repo already had a hand-rolled
browser loop (``browser_agent``) that clicks numbered elements one step at a
time. browser-use is the maintained project that does the same job with a
vision-capable loop, better failure recovery and an active community. The right
move is to drive it, not to keep rewriting a worse version of it.

Cost control
------------
browser-use sends a large prompt every step (the DOM plus history). Against a
free tier that is 8k tokens per minute, one naive step exceeds the whole budget -
measured: a single step requested 9,926 tokens. So the driver is built for small
models: no vision, no thinking, no planning, a trimmed element list, a short
history window and no judge. That measured run answered correctly within budget.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

# Where the isolated venv lives. Overridable so a deployment can point at its own.
DEFAULT_VENV = ".venv-browser"
DRIVER = Path(__file__).with_name("browser_use_driver.py")


@dataclass
class BrowserUseResult:
    ok: bool
    answer: str = ""
    steps: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    error: str = ""
    tokens: int = 0


def _venv_python(settings) -> Path | None:
    """Path to the isolated interpreter, or None when it is not installed."""
    configured = getattr(settings, "browser_use_venv", "") or DEFAULT_VENV
    root = Path(configured)
    if not root.is_absolute():
        root = Path.cwd() / root
    for candidate in (root / "bin" / "python", root / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return None


def is_available(settings) -> bool:
    return _venv_python(settings) is not None and DRIVER.exists()


def _llm_env(settings) -> dict[str, str]:
    """Provider keys and the model to use, resolved the same way the bot does.

    The driver picks a provider by which key is present, so a single configured
    provider is enough and the caller never has to name one.
    """
    env: dict[str, str] = {}
    for name in (
        "GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "HF_TOKEN",
    ):
        value = os.environ.get(name, "")
        if value:
            env[name] = value
    model = getattr(settings, "browser_use_model", "") or ""
    if model:
        env["BROWSER_USE_MODEL"] = model
    return env


async def run_browser_use(
    settings,
    *,
    url: str,
    task: str,
    user_id: int = 0,
    max_steps: int = 8,
    profile_dir: Path | None = None,
    timeout: int = 600,
) -> BrowserUseResult:
    """Drive the real browser-use agent and return its answer.

    The task is given the URL explicitly rather than asking the model to find it,
    which removes a whole class of wasted steps on small models.
    """
    python = _venv_python(settings)
    if python is None:
        return BrowserUseResult(
            False,
            error=(
                "browser-use belum terpasang. Jalankan: "
                "python -m venv .venv-browser && .venv-browser/bin/pip install "
                "\"browser-use @ git+https://github.com/browser-use/browser-use.git\""
            ),
        )

    out_dir = Path(getattr(settings, "outputs_path", "outputs")) / "browser"
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "url": url,
        "task": task,
        "max_steps": int(max_steps),
        "headless": bool(getattr(settings, "browser_headless", True)),
        "screenshot_dir": str(out_dir),
        "profile_dir": str(profile_dir) if profile_dir else "",
    }

    env = dict(os.environ)
    env.update(_llm_env(settings))
    # A profile with a real login is the point of reusing the bot's profile dir;
    # without this the browser starts signed out every time.
    env["BROWSER_USE_HEADLESS"] = "1" if payload["headless"] else "0"

    process = await asyncio.create_subprocess_exec(
        str(python), str(DRIVER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(Path.cwd()),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return BrowserUseResult(False, error=f"browser-use timeout setelah {timeout}s")

    text = (stdout or b"").decode("utf-8", "ignore")
    err = (stderr or b"").decode("utf-8", "ignore")

    # The driver prints exactly one JSON line, last, so a stray log line from a
    # library does not corrupt the result.
    data: dict = {}
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if not data:
        return BrowserUseResult(
            False, error=(err[-600:] or "driver tidak mengembalikan hasil")
        )

    return BrowserUseResult(
        ok=bool(data.get("ok")),
        answer=str(data.get("answer") or ""),
        steps=list(data.get("steps") or []),
        screenshots=list(data.get("screenshots") or []),
        error=str(data.get("error") or ""),
        tokens=int(data.get("tokens") or 0),
    )


def install_hint() -> str:
    return (
        "Pasang sekali di host:\n"
        "python -m venv .venv-browser\n"
        f"{shutil.which('bash') or ''} -c '.venv-browser/bin/pip install "
        '"browser-use @ git+https://github.com/browser-use/browser-use.git"\'\n'
        ".venv-browser/bin/python -m playwright install chromium"
    )
