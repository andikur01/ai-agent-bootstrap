"""The browser agent must show a pointer and must not stop at a login wall.

Two complaints from the user:

* "kursor nya keliatan ga?" - Playwright's clicks are invisible, so on a shared
  screen the page changed by itself.
* The agent must be able to ask for an email and password *while* it is working,
  save them, and sign in, instead of ending the task at the wall.

These tests drive the real methods with a small fake page, so the logic is
tested without launching a browser.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.account_vault import AccountVault
from app.services.browser_agent import BrowserAgent, Element


class FakeBox:
    def __init__(self, x, y, w, h):
        self._b = {"x": x, "y": y, "width": w, "height": h}

    def __getitem__(self, k):
        return self._b[k]


class FakeHandle:
    def __init__(self, box):
        self._box = box

    async def bounding_box(self):
        return self._box


class FakePage:
    """Records evaluate/selector calls; enough for the cursor and the wall."""

    def __init__(self, *, body: str = "", url: str = "https://situs.com/login", box=None):
        self.evaluated: list = []
        self._body = body
        self.url = url
        self._box = box or FakeBox(100, 200, 40, 20)
        self.filled: list[tuple[str, str]] = []
        self.clicked = False

    async def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        return None

    async def query_selector(self, selector):
        if "data-agent-idx" in selector:
            return FakeHandle(self._box)
        return None

    async def inner_text(self, selector):
        return self._body

    async def wait_for_timeout(self, ms):
        return None


class FakeGateway:
    def __init__(self):
        self.calls = 0

    async def complete(self, *a, **k):
        self.calls += 1
        raise AssertionError("the model must not be called in these tests")


def _agent(tmp_path: Path, *, vault=None, ask=None):
    class _S:
        browser_max_steps = 4

    return BrowserAgent(
        _S(), gateway=FakeGateway(), outputs_dir=tmp_path, vault=vault, ask=ask
    )


@pytest.mark.asyncio
async def test_installing_the_cursor_draws_a_pointer(tmp_path):
    agent = _agent(tmp_path)
    page = FakePage()
    await agent._install_cursor(page)
    assert page.evaluated, "the cursor script must run"
    assert "__agentCursor" in page.evaluated[0][0]


@pytest.mark.asyncio
async def test_moving_the_cursor_targets_the_element_centre(tmp_path):
    agent = _agent(tmp_path)
    page = FakePage(box=FakeBox(100, 200, 40, 20))
    await agent._move_cursor(page, Element(index=1, tag="button", label="Kirim"))

    moves = [arg for script, arg in page.evaluated if arg]
    assert moves, "the cursor must be moved to a coordinate"
    assert moves[-1] == [120.0, 210.0], "the pointer goes to the centre of the element"


@pytest.mark.asyncio
async def test_a_cursor_failure_never_breaks_the_task(tmp_path):
    agent = _agent(tmp_path)

    class _Broken(FakePage):
        async def evaluate(self, script, arg=None):
            raise RuntimeError("page navigated away")

    await agent._install_cursor(_Broken())  # must not raise
    await agent._move_cursor(_Broken(), Element(index=1, tag="button", label="x"))


@pytest.mark.asyncio
async def test_a_login_wall_asks_the_user_and_saves_the_account(tmp_path):
    """The heart of the feature: ask, save, sign in - inside the running task."""
    vault = AccountVault(tmp_path / "accounts.json")
    asked: list[str] = []

    async def ask(question: str) -> str:
        asked.append(question)
        return "situs.com aku@email.com rahasia123"

    agent = _agent(tmp_path, vault=vault, ask=ask)
    page = FakePage(body="please sign in to continue", url="https://situs.com/login")

    # Sign-in itself needs the harvester's form filling; stop it right after the
    # save so the test asserts on what we control.
    await agent._ask_for_account(page, "https://situs.com/login")

    assert asked, "the user must be asked for a login"
    assert "situs.com" in asked[0]
    saved = vault.get("situs.com")
    assert saved is not None, "the answer must be stored for reuse"
    assert saved.email == "aku@email.com"
    assert saved.password == "rahasia123"


@pytest.mark.asyncio
async def test_no_question_when_there_is_no_login_wall(tmp_path):
    """A public page must not trigger a pointless request for credentials."""
    vault = AccountVault(tmp_path / "accounts.json")
    asked: list[str] = []

    async def ask(question: str) -> str:
        asked.append(question)
        return ""

    agent = _agent(tmp_path, vault=vault, ask=ask)
    page = FakePage(body="welcome to our public homepage", url="https://situs.com/")

    await agent._ask_for_account(page, "https://situs.com/")
    assert asked == [], "a readable page needs no login"


@pytest.mark.asyncio
async def test_an_ignored_question_does_not_crash_the_task(tmp_path):
    vault = AccountVault(tmp_path / "accounts.json")

    async def ask(question: str) -> None:
        return None  # user never answered

    agent = _agent(tmp_path, vault=vault, ask=ask)
    page = FakePage(body="sign in required", url="https://situs.com/login")

    ok = await agent._ask_for_account(page, "https://situs.com/login")
    assert ok is False
    assert vault.get("situs.com") is None


@pytest.mark.asyncio
async def test_a_malformed_answer_is_rejected(tmp_path):
    """A one-word answer is not a login; storing it would be wrong."""
    vault = AccountVault(tmp_path / "accounts.json")

    async def ask(question: str) -> str:
        return "123456"  # a one-time code, not a login

    agent = _agent(tmp_path, vault=vault, ask=ask)
    page = FakePage(body="sign in required", url="https://situs.com/login")

    ok = await agent._ask_for_account(page, "https://situs.com/login")
    assert ok is False
    assert vault.get("situs.com") is None


@pytest.mark.asyncio
async def test_an_account_can_be_sent_without_the_host(tmp_path):
    """Free text means the user may type only 'email password'."""
    vault = AccountVault(tmp_path / "accounts.json")

    async def ask(question: str) -> str:
        return "aku@email.com rahasia123"

    agent = _agent(tmp_path, vault=vault, ask=ask)
    page = FakePage(body="sign in required", url="https://situs.com/login")

    await agent._ask_for_account(page, "https://situs.com/login")
    saved = vault.get("situs.com")
    assert saved is not None, "the host from the wall must be used when omitted"
    assert saved.email == "aku@email.com"
