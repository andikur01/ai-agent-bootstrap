"""Tests for the saved-login vault and the sign-in detection fix."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from app.services.account_vault import AccountVault, build_vault


@pytest.fixture()
def vault(tmp_path: Path) -> AccountVault:
    return AccountVault(tmp_path / "accounts.json")


def test_round_trip(vault: AccountVault) -> None:
    vault.put("google.com", "aku@gmail.com", "rahasia123")
    found = vault.get("google.com")
    assert found is not None
    assert found.email == "aku@gmail.com"
    assert found.password == "rahasia123"


def test_a_subdomain_finds_the_parent_login(vault: AccountVault) -> None:
    """One Google login should cover aistudio, not just google.com."""
    vault.put("google.com", "aku@gmail.com", "rahasia123")
    assert vault.get("aistudio.google.com") is not None
    assert vault.get("accounts.google.com") is not None


def test_an_unrelated_host_does_not_get_someone_elses_login(vault: AccountVault) -> None:
    vault.put("google.com", "aku@gmail.com", "rahasia123")
    assert vault.get("evil-google.com") is None
    assert vault.get("notgoogle.com") is None


def test_password_is_never_returned_in_full_by_describe(vault: AccountVault) -> None:
    vault.put("google.com", "aku@gmail.com", "rahasia123")
    described = " ".join(vault.describe())
    assert "rahasia123" not in described
    assert "aku@gmail.com" in described


def test_masked_password_keeps_only_the_ends(vault: AccountVault) -> None:
    account = vault.put("google.com", "aku@gmail.com", "rahasia123")
    masked = account.masked_password()
    assert masked.startswith("r") and masked.endswith("3")
    assert "ahasia12" not in masked


def test_file_is_owner_only(vault: AccountVault) -> None:
    vault.put("google.com", "aku@gmail.com", "rahasia123")
    mode = stat.S_IMODE(os.stat(vault.path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_forget_and_clear(vault: AccountVault) -> None:
    vault.put("google.com", "a@b.com", "x1")
    vault.put("groq.com", "a@b.com", "x2")
    assert vault.forget("google.com") is True
    assert vault.forget("google.com") is False
    assert vault.get("google.com") is None
    assert vault.get("groq.com") is not None
    assert vault.clear() == 1
    assert vault.hosts() == []


def test_survives_a_reload(vault: AccountVault, tmp_path: Path) -> None:
    vault.put("google.com", "aku@gmail.com", "rahasia123")
    again = AccountVault(tmp_path / "accounts.json")
    found = again.get("google.com")
    assert found is not None and found.password == "rahasia123"


def test_a_corrupt_file_does_not_crash_the_bot(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text("{ this is not json")
    vault = AccountVault(path)
    assert vault.hosts() == []


def test_a_partial_entry_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"google.com": {"email": "a@b.com"}, "ok.com": {"email": "c@d.com", "password": "p"}}))
    vault = AccountVault(path)
    assert vault.hosts() == ["ok.com"]


def test_vault_is_not_inside_the_repo_tracking(tmp_path: Path) -> None:
    """The vault must live under the gitignored data directory."""
    class S:
        data_dir = str(tmp_path / "data")
    built = build_vault(S())
    assert "data" in str(built.path)
    assert built.path.name == "accounts.json"


# --------------------------------------------------------------- sign-in check
class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


class FakePage:
    def __init__(self, url="", body=""):
        self.url = url
        self._body = body

    async def inner_text(self, _selector):
        return self._body


@pytest.mark.asyncio
async def test_a_tracking_cookie_is_not_a_login() -> None:
    """The bug the user hit: Google sets NID on first load, before any sign-in."""
    from app.services.key_harvester import KeyHarvester

    harvester = KeyHarvester.__new__(KeyHarvester)
    context = FakeContext([
        {"name": "NID", "domain": ".google.com"},
        {"name": "OTZ", "domain": "accounts.google.com"},
    ])
    page = FakePage(url="https://accounts.google.com/v3/signin/identifier", body="Sign in")
    assert await harvester._signed_in(context, page, "aistudio.google.com") is False


@pytest.mark.asyncio
async def test_a_real_session_cookie_is_a_login() -> None:
    from app.services.key_harvester import KeyHarvester

    harvester = KeyHarvester.__new__(KeyHarvester)
    context = FakeContext([
        {"name": "NID", "domain": ".google.com"},
        {"name": "SID", "domain": ".google.com"},
        {"name": "__Secure-1PSID", "domain": ".google.com"},
    ])
    page = FakePage(url="https://aistudio.google.com/apikey", body="Get API key")
    assert await harvester._signed_in(context, page, "aistudio.google.com") is True


@pytest.mark.asyncio
async def test_another_sites_session_cookie_does_not_count() -> None:
    from app.services.key_harvester import KeyHarvester

    harvester = KeyHarvester.__new__(KeyHarvester)
    context = FakeContext([{"name": "SID", "domain": ".example.com"}])
    page = FakePage(url="https://aistudio.google.com/apikey", body="")
    assert await harvester._signed_in(context, page, "aistudio.google.com") is False


@pytest.mark.asyncio
async def test_no_cookies_and_a_login_wall_is_not_signed_in() -> None:
    from app.services.key_harvester import KeyHarvester

    harvester = KeyHarvester.__new__(KeyHarvester)
    page = FakePage(url="https://example.com/login", body="Please log in")
    assert await harvester._signed_in(FakeContext([]), page, "example.com") is False


# ------------------------------------------------------- saved-account sign-in
class FakeField:
    def __init__(self):
        self.value = ""
        self.filled = 0

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def fill(self, value, timeout=None):
        self.value = value
        self.filled += 1


class FakeButton:
    def __init__(self):
        self.clicked = 0

    async def count(self):
        return 1

    async def click(self, timeout=None):
        self.clicked += 1


class SignInPage:
    """A page that mimics either a one-screen or two-screen login form."""

    def __init__(self, *, two_screen: bool):
        self.two_screen = two_screen
        self.identifier = FakeField()
        self.password = FakeField()
        self.button = FakeButton()
        self.password_visible = not two_screen
        self.submits = 0
        self.pressed_enter = 0

    # Playwright-shaped API
    def locator(self, selector):
        page = self

        class _L:
            def __init__(self, first):
                self._first = first

            @property
            def first(self):
                return self._first

        if "password" in selector:
            field = self.password if self.password_visible else None
        else:
            field = self.identifier

        if field is None:
            class _Empty:
                async def count(self):
                    return 0

                async def is_visible(self):
                    return False
            return _L(_Empty())
        return _L(field)

    def get_by_role(self, role, name=None):
        page = self

        class _R:
            def __init__(self, first):
                self._first = first

            @property
            def first(self):
                return self._first

        if role != "button":
            class _None:
                async def count(self):
                    return 0
            return _R(_None())

        # Only the generic submit names match; the site-specific ones do not.
        text = getattr(name, "pattern", str(name))
        if any(word in text for word in ("Next", "Login", "Sign in", "Submit", "Masuk", "Continue")):
            return _R(self.button)

        class _NoMatch:
            async def count(self):
                return 0
        return _R(_NoMatch())

    class _Keyboard:
        def __init__(self, page):
            self.page = page

        async def press(self, key):
            self.page.pressed_enter += 1

    @property
    def keyboard(self):
        return self._Keyboard(self)


@pytest.mark.asyncio
async def test_one_screen_form_fills_both_fields_then_submits_once(tmp_path: Path) -> None:
    """Submitting after the email alone sends a half-filled form; that bug
    surfaced as a wrong-credentials error on a valid login."""
    from app.services.key_harvester import KeyHarvester

    vault = AccountVault(tmp_path / "accounts.json")
    vault.put("example.com", "aku@example.com", "rahasia")
    harvester = KeyHarvester.__new__(KeyHarvester)
    harvester.vault = vault

    page = SignInPage(two_screen=False)
    ok = await harvester.sign_in_with_saved_account(page, "example.com")
    assert ok is True
    assert page.identifier.value == "aku@example.com"
    assert page.password.value == "rahasia"
    assert page.button.clicked == 1, "a one-screen form must be submitted exactly once"


@pytest.mark.asyncio
async def test_two_screen_form_fills_email_then_password(tmp_path: Path) -> None:
    """Google reveals the password field only after the email is submitted."""
    from app.services.key_harvester import KeyHarvester

    vault = AccountVault(tmp_path / "accounts.json")
    vault.put("google.com", "aku@gmail.com", "rahasia")
    harvester = KeyHarvester.__new__(KeyHarvester)
    harvester.vault = vault

    page = SignInPage(two_screen=True)

    async def reveal_after_first_submit(p, t=None):
        page.password_visible = True

    # The real code re-queries the page after submitting, so flip visibility
    # when the first click happens.
    original_click = page.button.click

    async def click(timeout=None):
        await original_click(timeout=timeout)
        page.password_visible = True

    page.button.click = click

    ok = await harvester.sign_in_with_saved_account(page, "google.com")
    assert ok is True
    assert page.identifier.value == "aku@gmail.com"
    assert page.password.value == "rahasia"
    assert page.button.clicked == 2


@pytest.mark.asyncio
async def test_no_saved_account_means_no_attempt(tmp_path: Path) -> None:
    from app.services.key_harvester import KeyHarvester

    vault = AccountVault(tmp_path / "accounts.json")
    harvester = KeyHarvester.__new__(KeyHarvester)
    harvester.vault = vault

    page = SignInPage(two_screen=False)
    assert await harvester.sign_in_with_saved_account(page, "example.com") is False
    assert page.button.clicked == 0


@pytest.mark.asyncio
async def test_a_page_with_no_login_form_is_left_alone(tmp_path: Path) -> None:
    from app.services.key_harvester import KeyHarvester

    vault = AccountVault(tmp_path / "accounts.json")
    vault.put("example.com", "a@b.com", "p")
    harvester = KeyHarvester.__new__(KeyHarvester)
    harvester.vault = vault

    class Blank:
        def locator(self, selector):
            class _L:
                @property
                def first(self):
                    class _E:
                        async def count(self):
                            return 0

                        async def is_visible(self):
                            return False
                    return _E()
            return _L()

    assert await harvester.sign_in_with_saved_account(Blank(), "example.com") is False
