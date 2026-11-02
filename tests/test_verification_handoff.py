"""End-to-end test of the one-time-code handoff, against a real browser.

No mocks. A real HTTP server serves a sign-in page that first asks for the
password and then demands a code sent "to the user's email". A real headless
Chromium fills the saved account, hits the code screen, and the ``ask`` hook is
what supplies the code - exactly the path a real provider takes.

The test asserts the thing that matters: the task finishes after the user
answers, instead of stopping and making them start over.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright")

CODE = "483920"
KEY = "AIza" + "C" * 35

# Two steps: password first, then a code field. The code page has no "create
# key" button, so it reads as a wall - which is what triggers the handoff.
_LOGIN_PAGE = """<!doctype html><html><body>
<h1>Sign in</h1>
<form method="get" action="/verify">
  <input type="email" name="email">
  <input type="password" name="password">
  <button type="submit">Sign in</button>
</form>
</body></html>"""

_VERIFY_PAGE = """<!doctype html><html><body>
<h1>Verify it is you</h1>
<p>We sent a code to your email.</p>
<form method="get" action="/keys">
  <input type="text" name="otp" autocomplete="one-time-code" inputmode="numeric">
  <button type="submit">Verify</button>
</form>
</body></html>"""

_KEYS_PAGE = f"""<!doctype html><html><body>
<h1>API Keys</h1>
<button id="create">Create new secret key</button>
<div id="out" style="display:none"><code>{KEY}</code></div>
<script>
document.getElementById('create').onclick = () => {{
  document.getElementById('out').style.display = 'block';
}};
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.startswith("/verify"):
            body = _VERIFY_PAGE
        elif self.path.startswith("/keys"):
            body = _KEYS_PAGE
        else:
            body = _LOGIN_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture()
def console():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


@dataclass
class _Settings:
    data_path: Path
    browser_max_steps: int = 8


def _harvester(tmp_path):
    from app.services.account_vault import AccountVault
    from app.services.browser_session import build_profiles
    from app.services.key_harvester import KeyHarvester

    settings = _Settings(data_path=tmp_path)
    profiles = build_profiles(settings)
    vault = AccountVault(tmp_path / "vault.json")
    return KeyHarvester(settings, profiles=profiles, outputs_dir=tmp_path / "out", vault=vault), vault


def _provider(console_url: str):
    from app.llm.credentials import Provider

    return Provider(
        name="gemini",
        env_var="GEMINI_API_KEY",
        label="Fake Gemini",
        signup_url=console_url,
        console_url=console_url,
        free_note="test",
        litellm_prefix="gemini/",
    )


def test_harvest_asks_for_the_code_and_then_finishes(console, tmp_path):
    """The whole point: the user answers once, the task completes."""
    import asyncio

    harvester, vault = _harvester(tmp_path)
    # The bot stores accounts under the slugified host, so the test must too.
    from app.services.browser_session import host_from_url

    vault.put(host_from_url(console), "me@example.com", "hunter2")

    asked: list[str] = []

    async def ask(question: str) -> str:
        asked.append(question)
        return CODE

    result = asyncio.run(harvester.harvest(_provider(console), user_id=1, ask=ask))

    assert asked, "the agent must ask for the code rather than give up"
    assert "kode verifikasi" in asked[0].lower()
    assert result.ok, result.message
    assert result.key == KEY


def test_without_the_ask_hook_a_code_wall_is_reported_not_guessed(console, tmp_path):
    """No handoff available means say so - never invent a code."""
    import asyncio

    harvester, vault = _harvester(tmp_path)
    from app.services.browser_session import host_from_url

    vault.put(host_from_url(console), "me@example.com", "hunter2")

    result = asyncio.run(harvester.harvest(_provider(console), user_id=2))

    assert not result.ok
    assert result.needs_login
