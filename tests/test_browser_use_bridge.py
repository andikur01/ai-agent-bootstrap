"""The browser-use bridge must degrade clearly, not silently.

browser-use runs in its own virtualenv because its pins conflict with the bot's
(pillow 13 -> 12, pydantic 2.14 -> 2.13). That isolation is only safe if the bot
notices when the venv is missing and says so, instead of pretending to browse and
returning nothing.

No model call happens here: these pin the wiring, the install hint, and the
availability check. The live agent is exercised by hand against a real page.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import Settings
from app.services import browser_use_service as bus


def test_missing_venv_reports_how_to_install(tmp_path, monkeypatch):
    """A missing specialist is an instruction, not a crash."""
    settings = Settings()
    monkeypatch.setattr(
        settings, "browser_use_venv", str(tmp_path / "does-not-exist"), raising=False
    )
    assert bus.is_available(settings) is False

    result = asyncio.run(
        bus.run_browser_use(settings, url="https://example.com", task="apa isinya")
    )
    assert result.ok is False
    assert "browser-use belum terpasang" in result.error
    assert "pip install" in result.error, "the error must say how to fix it"


def test_available_when_venv_python_exists(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    settings = Settings()
    monkeypatch.setattr(settings, "browser_use_venv", str(venv), raising=False)
    assert bus.is_available(settings) is True


def test_only_configured_keys_are_passed_to_the_driver(monkeypatch):
    """The driver picks a provider from the environment, so an empty key must
    not be exported as an empty string - that reads as "configured but blank"."""
    settings = Settings()
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = bus._llm_env(settings)
    assert env.get("GROQ_API_KEY") == "gsk_test"
    assert "GEMINI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_install_hint_names_the_isolated_venv():
    hint = bus.install_hint()
    assert ".venv-browser" in hint
    assert "playwright install chromium" in hint
