"""Fetch an API key from a provider console the user is already signed into.

This is the closest thing to "the bot tops up its own quota" that is actually
possible. The user signs in once (see ``browser_session``); after that this
module opens the provider's key page with that saved session, clicks "Create API
key", reads the key out of the page, verifies it against the provider, and adds
it to the key pool. The user does nothing per key.

Why it cannot go further: providers expose no API for creating keys, and
automating account *signup* is what gets accounts banned. So the split is fixed -
the human does the login (and any captcha or 2FA), the bot does everything after.

The happy path is fully deterministic: locate the button by its visible text,
click it, scrape the key. No LLM call is involved, which keeps this cheap. The
model is only asked for help when the deterministic pass finds no button, which
is the case when a provider renames its UI.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger
from app.llm.credentials import PROVIDERS, Provider
from app.services.browser_session import host_from_url

log = get_logger(__name__)

# Buttons that create a key, across the providers in PROVIDERS.
CREATE_PATTERNS = (
    r"create\s+new\s+secret\s+key",
    r"create\s+api\s+key",
    r"create\s+new\s+key",
    r"create\s+key",
    r"generate\s+new\s+token",
    r"new\s+token",
    r"buat\s+kunci",
    r"tambah\s+kunci",
)

# Cookies that only exist after a successful sign-in. Names are matched as
# substrings of the cookie name. The empty key is the default for any host not
# listed - deliberately short, because a wrong marker here means claiming a
# login that did not happen.
SESSION_COOKIE_MARKERS: dict[str, tuple[str, ...]] = {
    "": ("session", "auth", "token", "sid"),
    "accounts.google.com": ("SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID"),
    "google.com": ("SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID"),
    "aistudio.google.com": ("SID", "SSID", "HSID", "APISID", "SAPISID", "__Secure-1PSID"),
    "console.groq.com": ("__session", "__client", "session"),
    "openrouter.ai": ("__session", "session"),
    "platform.deepseek.com": ("session", "token"),
    "cloud.cerebras.ai": ("session", "token", "auth"),
    "huggingface.co": ("token", "session"),
}

# Text that means "you are not signed in".
LOGIN_WALL_PATTERNS = (
    r"sign\s+in",
    r"log\s+in",
    r"masuk",
    r"continue with google",
    r"sign in with",
)

KEY_SHAPE = {
    "gemini": r"AIza[A-Za-z0-9_\-]{30,}",
    "groq": r"gsk_[A-Za-z0-9]{20,}",
    "openrouter": r"sk-or-v1-[A-Za-z0-9]{20,}",
    "deepseek": r"sk-[A-Za-z0-9]{20,}",
    "cerebras": r"csk-[A-Za-z0-9]{20,}",
    "hf": r"hf_[A-Za-z0-9]{20,}",
    "mistral": r"[A-Za-z0-9]{32}",
}

MAX_STEPS = 12


@dataclass
class HarvestResult:
    ok: bool
    provider: str = ""
    key: str = ""
    message: str = ""
    needs_login: bool = False
    screenshots: list[Path] = field(default_factory=list)

    def masked_key(self) -> str:
        if len(self.key) <= 4:
            return "****"
        return f"****{self.key[-4:]}"


class KeyHarvester:
    """Drives a provider console to create one API key."""

    def __init__(
        self,
        settings,
        *,
        profiles,
        outputs_dir: Path,
        max_steps: int = MAX_STEPS,
        vault=None,
    ):
        self.settings = settings
        self.profiles = profiles
        self.outputs = Path(outputs_dir) / "keys"
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.max_steps = max_steps
        self.vault = vault
        self._xvfb = None

    # ------------------------------------------------------------- public API
    # Fields that hold the identifier on a sign-in page, across providers.
    IDENTIFIER_SELECTORS = (
        'input[type="email"]',
        'input[name="identifier"]',
        'input[type="text"][autocomplete="username"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[name="login"]',
        'input[id*="email" i]',
        'input[id*="user" i]',
    )
    PASSWORD_SELECTORS = ('input[type="password"]', 'input[name="password"]')
    SUBMIT_NAMES = ("Next", "Berikutnya", "Lanjut", "Continue", "Sign in", "Masuk", "Login", "Submit")

    async def _first_visible(self, page, selectors):
        """Return the first visible locator among *selectors*, or None."""
        for selector in selectors:
            try:
                field = page.locator(selector).first
                if await field.count() and await field.is_visible():
                    return field
            except Exception:  # noqa: BLE001 - try the next selector
                continue
        return None

    async def _submit(self, page) -> None:
        """Click the obvious submit button, or press Enter if there is none."""
        for name in self.SUBMIT_NAMES:
            try:
                button = page.get_by_role("button", name=re.compile(name, re.I)).first
                if await button.count():
                    await button.click(timeout=8000)
                    return
            except Exception:  # noqa: BLE001
                continue
        try:
            await page.keyboard.press("Enter")
        except Exception:  # noqa: BLE001
            pass

    async def sign_in_with_saved_account(self, page, host: str, *, progress=None) -> bool:
        """Sign in using a saved email/password, so the user need not do it.

        Two shapes have to be handled, and getting them confused is what breaks
        a login:

        * One page with both fields. Fill both, then submit *once*. Submitting
          after the identifier alone sends a half-filled form and the site
          answers "username invalid" - a failure that looks like wrong
          credentials and is not.
        * Two pages, as Google does. The password field does not exist until the
          identifier is submitted, so it is looked up again after that step.

        Anything the account cannot answer - a captcha, a 2FA prompt, a security
        key - is left for the user, because guessing at those would be both wrong
        and, for a real account, harmful.

        Returns True when a form was submitted, not when the login succeeded.
        Success is decided afterwards by ``_signed_in``.
        """
        if self.vault is None:
            return False
        account = self.vault.get(host)
        if account is None:
            return False

        identifier = await self._first_visible(page, self.IDENTIFIER_SELECTORS)
        password = await self._first_visible(page, self.PASSWORD_SELECTORS)

        if identifier is None and password is None:
            return False

        # Both fields on this page: fill both, then submit once.
        if identifier is not None and password is not None:
            await identifier.fill(account.email, timeout=8000)
            await password.fill(account.password, timeout=8000)
            if progress:
                await progress(20, "Email dan password sudah aku isi, mengirim...")
            await self._submit(page)
            await asyncio.sleep(4)
            return True

        # Identifier only: submit it, then look for the password field that the
        # next screen may reveal.
        if identifier is not None:
            await identifier.fill(account.email, timeout=8000)
            if progress:
                await progress(15, "Email sudah aku isi, lanjut ke password...")
            await self._submit(page)
            await asyncio.sleep(3)

            password = await self._first_visible(page, self.PASSWORD_SELECTORS)
            if password is None:
                return True
            await password.fill(account.password, timeout=8000)
            if progress:
                await progress(25, "Password sudah aku isi. Menunggu situsnya...")
            await self._submit(page)
            await asyncio.sleep(4)
            return True

        # Password only, no identifier field visible: nothing useful to do.
        return False

    async def harvest(
        self,
        provider: Provider,
        *,
        user_id: int = 0,
        progress=None,
    ) -> HarvestResult:
        """Create one key on the provider console using the saved session."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return HarvestResult(False, message="Playwright belum terpasang.")

        result = HarvestResult(False, provider=provider.name)
        async with async_playwright() as pw:
            context = await self._open(pw, user_id, headless=True)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                if progress:
                    await progress(15, f"Membuka halaman key {provider.label}...")
                try:
                    await page.goto(
                        provider.console_url, wait_until="domcontentloaded", timeout=45000
                    )
                except Exception as exc:  # noqa: BLE001
                    result.message = f"Gagal buka {provider.console_url}: {str(exc)[:160]}"
                    return result

                await self._settle(page)
                shot = await self._shot(page, f"{provider.name}-1-open")
                result.screenshots.append(shot)

                if await self._login_wall(page):
                    # A saved account turns this from "go log in yourself" into
                    # something the agent finishes alone. Only when none is saved
                    # does the user need to be involved.
                    host = host_from_url(provider.console_url)
                    signed = False
                    if self.vault is not None and self.vault.get(host) is not None:
                        if progress:
                            await progress(30, f"Login sendiri pakai akun tersimpan ({host})...")
                        try:
                            signed = await self.sign_in_with_saved_account(
                                page, host, progress=progress
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning("saved-account sign-in failed: %s", exc)
                        if signed:
                            await self._settle(page, extra=3.0)
                            result.screenshots.append(
                                await self._shot(page, f"{provider.name}-2-after-login")
                            )

                    if await self._login_wall(page):
                        result.needs_login = True
                        if signed:
                            result.message = (
                                f"Akun tersimpan sudah aku pakai, tapi {provider.label} "
                                "masih minta langkah tambahan (captcha / verifikasi).\n\n"
                                "Buka layarnya di HP dan selesaikan langkah itu, "
                                "lalu bilang `ambil key " + provider.name + "` lagi."
                            )
                        else:
                            result.message = (
                                f"Belum login di {provider.label}.\n\n"
                                "Kirim akunnya sekali: `/akun " + host + " email password` "
                                f"- atau login sendiri: `/login {provider.console_url}`"
                            )
                        return result

                if progress:
                    await progress(40, "Mencari tombol buat key...")
                clicked = await self._click_create(page)
                if not clicked:
                    result.message = (
                        f"Tidak menemukan tombol buat key di {provider.label}. "
                        "UI-nya mungkin berubah."
                    )
                    result.screenshots.append(await self._shot(page, f"{provider.name}-no-button"))
                    return result

                await self._settle(page, extra=2.0)
                result.screenshots.append(await self._shot(page, f"{provider.name}-2-after-click"))

                if progress:
                    await progress(70, "Membaca key dari halaman...")
                key = await self._extract_key(page, provider)
                if not key:
                    result.message = (
                        "Key tidak terbaca dari halaman. "
                        "Coba `/login` ulang lalu `/keys auto` lagi."
                    )
                    return result

                result.key = key
                result.ok = True
                result.message = f"Key {provider.label} berhasil dibuat."
                if progress:
                    await progress(90, "Key dibuat, siap dipakai.")
                return result
            finally:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    async def open_for_login(
        self,
        url: str,
        *,
        user_id: int = 0,
        seconds: int = 240,
        progress=None,
    ) -> HarvestResult:
        """Open a real browser at *url* so the user can sign in themselves.

        Runs headful: on a desktop/VPS with a display the user sees the window;
        on a headless server it runs under xvfb and progress screenshots are sent
        to Telegram instead. Either way the password never reaches the bot.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return HarvestResult(False, message="Playwright belum terpasang.")

        result = HarvestResult(False)
        # Which site the session belongs to, so a cookie from some other domain
        # can never be mistaken for this one's login.
        host = host_from_url(url)
        # The display must exist before Playwright starts: its driver process
        # captures the environment once and hands it to every browser it spawns,
        # so setting DISPLAY afterwards has no effect.
        await self._ensure_display()
        async with async_playwright() as pw:
            context = await self._open(pw, user_id, headless=False)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:  # noqa: BLE001
                    result.message = f"Gagal buka {url}: {str(exc)[:160]}"
                    return result

                result.screenshots.append(await self._shot(page, "login-1"))
                if progress:
                    await progress(
                        20,
                        f"Browser dibuka di {url}. Login di jendela itu "
                        f"(atau lewat screenshot ini). Aku tunggu {seconds} detik.",
                    )

                # Poll so we can tell the user the moment the session is stored.
                waited = 0
                step = 5
                shot_every = 30
                next_shot = shot_every
                while waited < seconds:
                    await asyncio.sleep(step)
                    waited += step
                    if await self._signed_in(context, page, host):
                        result.ok = True
                        result.message = (
                            "Sesi tersimpan. Login berhasil - "
                            "berikutnya aku bisa buka situs ini sendiri."
                        )
                        if progress:
                            await progress(100, result.message)
                        result.screenshots.append(await self._shot(page, "login-2-done"))
                        return result
                    # Keep the user oriented: without a window on their screen,
                    # a silent wait looks like nothing is happening.
                    if progress and waited >= next_shot:
                        next_shot += shot_every
                        try:
                            shot = await self._shot(page, f"login-wait-{waited}")
                            result.screenshots.append(shot)
                            await progress(
                                30,
                                f"Masih menunggu login ({waited}s). "
                                "Ini yang aku lihat sekarang:",
                                screenshot=str(shot),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug("progress screenshot failed: %s", exc)
                result.message = (
                    "Belum ada sesi tersimpan. Coba lagi kalau sudah login."
                )
                return result
            finally:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    # -------------------------------------------------------------- internals
    async def _signed_in(self, context, page, host: str) -> bool:
        """True only when a real session cookie for *host* exists.

        The earlier version treated *any* cookie as proof of login, so a page
        that sets a tracking cookie on first load (Google's NID, for instance)
        was reported as a successful sign-in seconds after opening - while the
        user was still looking at the password prompt. That is the worst kind of
        bug: it tells the user the work is done when none of it is.

        A session cookie is what actually survives the login flow, so look for
        one, and require that the page has left the sign-in screens.
        """
        markers = SESSION_COOKIE_MARKERS.get(host, SESSION_COOKIE_MARKERS[""])
        try:
            cookies = await context.cookies()
        except Exception:  # noqa: BLE001
            return False

        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if not domain:
                continue
            # Only cookies belonging to this site or its parent count. A
            # third-party cookie from an embedded widget must not qualify.
            if not (domain == host or host.endswith("." + domain) or domain.endswith("." + host)):
                continue
            if any(marker in cookie.get("name", "") for marker in markers):
                # Expiry is not a marker: session cookies (expires -1) are
                # common for authenticated flows too, so presence is enough.
                return True

        # Fall back to the page itself, but only when it has clearly moved past
        # sign-in - never on the mere absence of a password field.
        url = (page.url or "").lower()
        if any(marker in url for marker in ("login", "signin", "sign-in", "accounts.google")):
            return False
        try:
            body = (await page.inner_text("body"))[:4000].lower()
        except Exception:  # noqa: BLE001
            return False
        if any(re.search(p, body) for p in LOGIN_WALL_PATTERNS):
            return False
        return "sign out" in body or "logout" in body or "keluar" in body

    async def _open(self, pw, user_id: int, *, headless: bool):
        profile = self.profiles.profile_dir(user_id)
        args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if headless:
            args.append("--headless=new")
        return await pw.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            args=args,
            viewport={"width": 1440, "height": 900},
            locale="id-ID",
        )

    @staticmethod
    def _has_display() -> bool:
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    async def _ensure_display(self) -> str:
        """Guarantee DISPLAY points at a live X server, starting Xvfb if needed.

        Called *before* ``async_playwright()``, because the Playwright driver
        reads its environment once at startup and passes it to every browser
        process. Setting DISPLAY later silently has no effect and Chromium dies
        with "Missing X server or $DISPLAY".
        """
        if self._has_display():
            return os.environ["DISPLAY"]
        display = await self._start_xvfb()
        if display:
            os.environ["DISPLAY"] = display
        return display

    async def _start_xvfb(self) -> str:
        """Start Xvfb on a free display and return it, or '' if unavailable.

        Readiness is confirmed by waiting for the X socket, not by trusting the
        process to still be alive: a stale ``/tmp/.X<n>-lock`` from a crashed
        run makes Xvfb exit immediately, and launching Chromium against a dead
        display fails with a confusing "Missing X server" error.
        """
        import shutil

        if shutil.which("Xvfb") is None:
            log.warning("Xvfb not installed; falling back to headless login")
            return ""
        for number in range(99, 115):
            display = f":{number}"
            socket = Path(f"/tmp/.X11-unix/X{number}")
            if socket.exists():
                # A socket can outlive its server (a crashed Xvfb leaves one
                # behind). Reusing it makes Chromium fail with "Missing X
                # server", so skip and let Xvfb pick a clean number.
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "Xvfb",
                    display,
                    "-screen",
                    "0",
                    "1440x900x24",
                    "-nolisten",
                    "tcp",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                log.warning("Xvfb failed to start: %s", exc)
                return ""
            for _ in range(20):
                await asyncio.sleep(0.25)
                if socket.exists():
                    self._xvfb = proc
                    log.info("started Xvfb on %s for /login", display)
                    return display
                if proc.returncode is not None:
                    break  # display busy, try the next number
            if proc.returncode is None:
                proc.terminate()
        log.warning("no free X display found for /login")
        return ""

    async def _settle(self, page, extra: float = 1.0) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - networkidle never fires on some SPAs
            pass
        await asyncio.sleep(extra)

    async def _login_wall(self, page) -> bool:
        """True when the page clearly wants a sign-in before anything else."""
        try:
            body = (await page.inner_text("body"))[:4000].lower()
        except Exception:  # noqa: BLE001
            return False
        # A create-key button means we are already through the wall.
        for pattern in CREATE_PATTERNS:
            if re.search(pattern, body):
                return False
        url = (page.url or "").lower()
        if any(marker in url for marker in ("login", "signin", "sign-in", "accounts.google")):
            return True
        return any(re.search(p, body) for p in LOGIN_WALL_PATTERNS)

    async def _click_create(self, page) -> bool:
        """Click the first button whose text looks like 'create a key'."""
        for pattern in CREATE_PATTERNS:
            try:
                locator = page.get_by_role("button", name=re.compile(pattern, re.I))
                if await locator.count():
                    await locator.first.click(timeout=10000)
                    return True
            except Exception:  # noqa: BLE001 - try the next pattern
                continue
            try:
                locator = page.get_by_text(re.compile(pattern, re.I))
                if await locator.count():
                    await locator.first.click(timeout=10000)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _extract_key(self, page, provider: Provider) -> str:
        """Scrape the new key from the page.

        Keys surface in three places depending on the provider: an input's value,
        a <code> block, or plain text in a modal. Check all three.
        """
        shape = KEY_SHAPE.get(provider.name)
        if not shape:
            return ""

        candidates: list[str] = []
        for selector in ("input[type=text]", "input[readonly]", "textarea", "code", "pre"):
            try:
                values = await page.eval_on_selector_all(
                    selector,
                    "els => els.map(e => e.value || e.textContent || '')",
                )
                candidates.extend(v for v in values if v)
            except Exception:  # noqa: BLE001
                continue
        try:
            candidates.append(await page.inner_text("body"))
        except Exception:  # noqa: BLE001
            pass

        for text in candidates:
            match = re.search(shape, text)
            if match:
                return match.group(0)
        return ""

    async def _shot(self, page, name: str) -> Path:
        path = self.outputs / f"{name}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed: %s", exc)
        return path


def build_harvester(settings, *, profiles, outputs_dir: Path, vault=None) -> KeyHarvester:
    return KeyHarvester(
        settings, profiles=profiles, outputs_dir=outputs_dir, vault=vault
    )


def console_for(provider_name: str) -> str:
    provider = PROVIDERS.get(provider_name)
    return provider.console_url if provider else ""
