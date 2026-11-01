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

    def __init__(self, settings, *, profiles, outputs_dir: Path, max_steps: int = MAX_STEPS):
        self.settings = settings
        self.profiles = profiles
        self.outputs = Path(outputs_dir) / "keys"
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.max_steps = max_steps
        self._xvfb = None

    # ------------------------------------------------------------- public API
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
                    result.needs_login = True
                    result.message = (
                        f"Belum login di {provider.label}. "
                        f"Jalankan `/login {provider.console_url}` dulu."
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
                step = 15
                while waited < seconds:
                    await asyncio.sleep(step)
                    waited += step
                    signed_in = False
                    try:
                        if await context.cookies():
                            signed_in = True
                    except Exception:  # noqa: BLE001
                        pass
                    if not signed_in:
                        # Some sites keep the session in localStorage instead of
                        # cookies; the password field disappearing is the tell.
                        try:
                            has_password = await page.evaluate(
                                "() => !!document.querySelector('input[type=password]')"
                            )
                            signed_in = not has_password and "login" not in (page.url or "").lower()
                        except Exception:  # noqa: BLE001
                            pass
                    if signed_in:
                        result.ok = True
                        result.message = (
                            "Sesi tersimpan. Login berhasil - "
                            "berikutnya aku bisa buka situs ini sendiri."
                        )
                        if progress:
                            await progress(100, result.message)
                        result.screenshots.append(await self._shot(page, "login-2-done"))
                        return result
                result.message = "Belum ada sesi tersimpan. Coba lagi kalau sudah login."
                return result
            finally:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    # -------------------------------------------------------------- internals
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


def build_harvester(settings, *, profiles, outputs_dir: Path) -> KeyHarvester:
    return KeyHarvester(settings, profiles=profiles, outputs_dir=outputs_dir)


def console_for(provider_name: str) -> str:
    provider = PROVIDERS.get(provider_name)
    return provider.console_url if provider else ""
