"""End-to-end test of key harvesting against a real (fake) provider console.

No mocks: a real HTTP server serves a real page with a real "Create new secret
key" button, and a real headless Chromium clicks it and scrapes the key. This is
the only way to know the selectors and the click actually work.

Skipped when Playwright's browser is not installed, so the unit suite still runs
in a bare environment.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright")

KEY = "AIza" + "T" * 35

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

_LOGIN_PAGE = """<!doctype html><html><body>
<h1>Sign in</h1><input type="email"><button>Continue with Google</button>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        body = _LOGIN_PAGE if self.path.startswith("/login") else _KEYS_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):  # keep the test output clean
        pass


@pytest.fixture()
def console():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


@dataclass
class _Settings:
    data_path: Path
    browser_max_steps: int = 8


def _harvester(tmp_path):
    from app.services.browser_session import build_profiles
    from app.services.key_harvester import KeyHarvester

    settings = _Settings(data_path=tmp_path)
    profiles = build_profiles(settings)
    return KeyHarvester(settings, profiles=profiles, outputs_dir=tmp_path / "out")


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


def test_harvest_creates_and_reads_a_key(console, tmp_path):
    import asyncio

    harvester = _harvester(tmp_path)
    result = asyncio.run(harvester.harvest(_provider(console), user_id=1))

    assert result.ok, result.message
    assert result.key == KEY
    assert result.masked_key() == "****" + KEY[-4:]
    assert result.screenshots, "at least one milestone screenshot expected"


def test_harvest_reports_login_wall_instead_of_failing(console, tmp_path):
    """When signed out, the bot must say so - not scrape a blank page."""
    import asyncio

    harvester = _harvester(tmp_path)
    result = asyncio.run(harvester.harvest(_provider(f"{console}/login"), user_id=2))

    assert not result.ok
    assert result.needs_login
    assert "/login" in result.message
