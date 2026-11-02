"""Entry point that runs *inside* the browser-use virtualenv.

Invoked as a subprocess by ``browser_use_service``. Reads one JSON request on
stdin, runs the real browser-use agent, prints one JSON result on stdout. It is
deliberately tiny and imports nothing from the bot, so it can live in a venv with
a completely different dependency set.

The tuning here is the whole point. browser-use defaults are built for a paid
model with a large context; the free tier this runs on allows 8k tokens per
minute, and a default step measured 9,926 tokens - the request was rejected
before the model ever saw it. Everything below exists to keep a step inside that
budget:

* ``use_vision=False`` - screenshots are the biggest single cost, and the DOM
  text already says what is on the page.
* ``use_thinking``/``enable_planning``/``use_judge`` off - each is a second model
  call per step.
* ``max_clickable_elements_length`` trimmed - the element list dominates the
  prompt.
* ``max_history_items`` short - history grows every step and is resent in full.
* ``flash_mode`` - asks for the action only, no commentary.

One provider is chosen by whichever key is present, so the caller passes keys and
nothing else.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Chromium refuses to start as root without these. In a container the bot runs as
# root, and without them the launch dies before CDP is up - the error reads
# "Browser process exited before CDP became available", which looks like a
# missing browser rather than a missing flag.
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-setuid-sandbox",
]


def _build_llm():
    """Pick a provider from whichever key is set. Order is by free-tier size."""
    if os.environ.get("GEMINI_API_KEY"):
        from browser_use import ChatGoogle

        model = os.environ.get("BROWSER_USE_MODEL") or "gemini-2.5-flash"
        return ChatGoogle(model=model, api_key=os.environ["GEMINI_API_KEY"])
    if os.environ.get("GROQ_API_KEY"):
        from browser_use import ChatGroq

        model = os.environ.get("BROWSER_USE_MODEL") or "openai/gpt-oss-20b"
        return ChatGroq(model=model, api_key=os.environ["GROQ_API_KEY"])
    if os.environ.get("OPENAI_API_KEY"):
        from browser_use import ChatOpenAI

        model = os.environ.get("BROWSER_USE_MODEL") or "gpt-4o-mini"
        return ChatOpenAI(model=model, api_key=os.environ["OPENAI_API_KEY"])
    if os.environ.get("ANTHROPIC_API_KEY"):
        from browser_use import ChatAnthropic

        model = os.environ.get("BROWSER_USE_MODEL") or "claude-3-5-haiku-latest"
        return ChatAnthropic(model=model, api_key=os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("OPENROUTER_API_KEY"):
        from browser_use import ChatOpenRouter

        model = os.environ.get("BROWSER_USE_MODEL") or "google/gemini-2.0-flash-exp:free"
        return ChatOpenRouter(model=model, api_key=os.environ["OPENROUTER_API_KEY"])
    return None


async def _main(request: dict) -> dict:
    from browser_use import Agent, Browser

    llm = _build_llm()
    if llm is None:
        return {"ok": False, "error": "tidak ada API key LLM yang tersedia untuk browser-use"}

    headless = bool(request.get("headless", True))
    profile_dir = request.get("profile_dir") or ""
    shot_dir = Path(request.get("screenshot_dir") or ".")
    shot_dir.mkdir(parents=True, exist_ok=True)

    browser_kwargs: dict = {
        "headless": headless,
        "chromium_sandbox": False,
        "args": CHROMIUM_ARGS,
    }
    # Reuse the bot's saved profile so a site the user already signed into stays
    # signed in. Without a profile the browser starts clean and every login wall
    # is hit again from scratch.
    if profile_dir:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        browser_kwargs["user_data_dir"] = profile_dir

    browser = Browser(**browser_kwargs)
    url = request.get("url") or ""
    task = request.get("task") or ""
    full_task = (
        f"Buka {url} lalu: {task}\n"
        "Jawab dalam bahasa Indonesia, ringkas dan langsung ke inti."
    ) if url else task

    shots: list[str] = []

    def on_step(state, output, step_no):
        # A screenshot per step is what the user sees as progress in the chat.
        try:
            import asyncio as _a

            async def _grab():
                data = await browser.take_screenshot()
                path = shot_dir / f"step-{step_no:02d}.png"
                path.write_bytes(data)
                shots.append(str(path))

            _a.get_event_loop().create_task(_grab())
        except Exception:  # noqa: BLE001 - a missing screenshot must not stop the run
            pass

    agent = Agent(
        task=full_task,
        llm=llm,
        browser=browser,
        use_vision=False,
        use_thinking=False,
        enable_planning=False,
        use_judge=False,
        flash_mode=True,
        include_attributes=[],
        max_clickable_elements_length=6000,
        max_actions_per_step=3,
        max_history_items=6,
        message_compaction=False,
        register_new_step_callback=on_step,
    )

    try:
        history = await agent.run(max_steps=int(request.get("max_steps") or 8))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    finally:
        try:
            await browser.kill()
        except Exception:  # noqa: BLE001
            pass

    answer = ""
    try:
        answer = history.final_result() or ""
    except Exception:  # noqa: BLE001
        answer = ""

    steps: list[str] = []
    try:
        for i, item in enumerate(history.history or [], start=1):
            actions = getattr(item, "action", None) or []
            for action in actions:
                for name, value in (action.model_dump(exclude_none=True) or {}).items():
                    steps.append(f"{i}. {name} {value}")
    except Exception:  # noqa: BLE001
        pass

    tokens = 0
    try:
        if history.usage:
            tokens = int(history.usage.total_tokens or 0)
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": bool(answer),
        "answer": answer,
        "steps": steps[:40],
        "screenshots": shots,
        "tokens": tokens,
    }


def main() -> None:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"permintaan tidak valid: {exc}"}))
        return
    try:
        result = asyncio.run(_main(request))
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
