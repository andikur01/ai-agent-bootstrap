"""Find API keys on any site the user names, not just the known providers.

The user's request was blunt: "kalau ada web baru yg menyediakan api key banyak,
gua suruh ke sana, bisa ga bro?" The honest answer used to be no - the harvester
knew seven consoles and nothing else. A site nobody had hard-coded was
unreachable, which made the whole feature look broken the moment the user tried
a site we had not heard of.

This module removes the list as a precondition. It opens whatever the user names,
signs in with a saved account if one matches, looks for anything shaped like a
key, and reports what it found - with the values, because they are the user's own
keys and the point is to stop them copying by hand.

What it deliberately does not do is store anything. Discovery and storage are
separated so the user can look at the result and approve it first; a scraper that
writes straight into .env is one bad pattern away from writing a password into a
credential slot.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

# Shapes that identify a key with confidence. Order matters only for labelling:
# a value matching several patterns gets the first, most specific name.
KNOWN_SHAPES: list[tuple[str, str]] = [
    ("gemini", r"AIza[A-Za-z0-9_\-]{35}"),
    ("groq", r"gsk_[A-Za-z0-9]{40,}"),
    ("openrouter", r"sk-or-v1-[a-f0-9]{64}"),
    ("anthropic", r"sk-ant-[A-Za-z0-9_\-]{40,}"),
    ("openai", r"sk-proj-[A-Za-z0-9_\-]{40,}"),
    ("deepseek", r"sk-[a-f0-9]{32}"),
    ("cerebras", r"csk-[A-Za-z0-9]{40,}"),
    ("huggingface", r"hf_[A-Za-z0-9]{30,}"),
    ("replicate", r"r8_[A-Za-z0-9]{30,}"),
    ("stability", r"sk-[A-Za-z0-9]{48}"),
    ("elevenlabs", r"sk_[a-f0-9]{48,}"),
    ("mistral", r"[A-Za-z0-9]{32}"),
    ("together", r"[a-f0-9]{64}"),
    ("generic", r"sk-[A-Za-z0-9_\-]{20,}"),
]

# Text near a key that makes it worth reporting. A 32-character hex string is
# everywhere - session ids, hashes, cache keys - so a bare match is not evidence
# of anything. A match sitting next to the word "api key" is.
_KEY_CONTEXT = re.compile(
    r"(api[\s_-]?key|secret[\s_-]?key|access[\s_-]?token|bearer|api[\s_-]?token)",
    re.I,
)

# Words that mean the value beside them is *not* a credential, whatever else the
# line says. Found the hard way: "Session: 7f3a..." was reported as a key because
# the page also contained the word "credentials" nearby.
_NOT_A_KEY = re.compile(
    r"(session|csrf|xsrf|cache|hash|checksum|signature|nonce|fingerprint|"
    r"request[\s_-]?id|trace[\s_-]?id|correlation)",
    re.I,
)

# Sites that produce a key on a page we can recognise.
KEY_PAGE_HINTS = re.compile(
    r"(api[\s_-]?key|apikey|api[\s_-]?token|access[\s_-]?token|"
    r"kredensial|credentials|create[\s_-]?key|generate[\s_-]?key|"
    r"new[\s_-]?token)",
    re.I,
)


@dataclass
class FoundKey:
    """One candidate key, with where it came from."""

    value: str
    kind: str
    host: str
    source: str = ""  # the page it was seen on

    def masked(self) -> str:
        if len(self.value) <= 8:
            return "****"
        return f"{self.value[:6]}...{self.value[-4:]}"

    def line(self) -> str:
        return f"`{self.value}` — {self.kind} ({self.host})"


@dataclass
class DiscoveryResult:
    ok: bool
    url: str = ""
    host: str = ""
    keys: list[FoundKey] = field(default_factory=list)
    message: str = ""
    needs_login: bool = False
    screenshots: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        if not self.keys:
            return self.message or "Tidak ada key yang aku temukan di halaman itu."
        lines = [f"Aku temukan {len(self.keys)} key di {self.host}:"]
        lines += [f"• {k.line()}" for k in self.keys]
        return "\n".join(lines)


def looks_like_key(value: str) -> tuple[str, str] | None:
    """Return (kind, value) if *value* is shaped like a key, else None.

    Matching is anchored: the whole string has to be the key, because a 64-char
    hex run inside a longer string is far more likely to be a hash or a session
    id than a credential.
    """
    candidate = (value or "").strip().strip("\"'`")
    if not (16 <= len(candidate) <= 200):
        return None
    for kind, pattern in KNOWN_SHAPES:
        if re.fullmatch(pattern, candidate):
            return kind, candidate
    return None


def scan_text_for_keys(text: str) -> list[tuple[str, str]]:
    """Find key-shaped strings in a page's text, keeping only plausible ones.

    Weak shapes - a bare 32- or 64-character hex run - need positive evidence:
    a nearby word like "api key", and no word like "session" that says the value
    is something else. Both conditions were added after a real page produced a
    session id as a key, and a wrong key in .env is worse than a missed one: it
    looks like capacity the bot does not have.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    weak = {"mistral", "together"}
    for kind, pattern in KNOWN_SHAPES:
        for match in re.finditer(rf"(?<![A-Za-z0-9_\-]){pattern}(?![A-Za-z0-9_\-])", text):
            value = match.group(0)
            if value in seen:
                continue
            if kind in weak:
                window = text[max(0, match.start() - 160) : match.end() + 160]
                if not _KEY_CONTEXT.search(window):
                    continue
                if _NOT_A_KEY.search(window):
                    continue
            seen.add(value)
            found.append((kind, value))
    return found


class KeyDiscovery:
    """Opens a site and reports the keys it can see there."""

    def __init__(
        self, settings, *, profiles=None, vault=None, live_view=None, outputs_dir: Path
    ):
        self.settings = settings
        self.profiles = profiles
        self.vault = vault
        self.live_view = live_view
        self.outputs = Path(outputs_dir) / "keys"
        self.outputs.mkdir(parents=True, exist_ok=True)

    async def _display(self) -> bool:
        if self.live_view is None:
            return False
        try:
            state = await self.live_view.ensure()
        except Exception:  # noqa: BLE001
            return False
        if state.running and state.display:
            import os

            os.environ["DISPLAY"] = state.display
            return True
        return False

    async def discover(
        self,
        url: str,
        *,
        user_id: int = 0,
        progress=None,
        sign_in: bool = True,
    ) -> DiscoveryResult:
        """Open *url*, sign in if we can, and report every key on the page."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return DiscoveryResult(False, message="Playwright belum terpasang.")

        from app.services.browser_session import host_from_url

        host = host_from_url(url)
        result = DiscoveryResult(ok=False, url=url, host=host)
        headful = await self._display()

        async def shot(page, name: str) -> None:
            try:
                path = self.outputs / f"{name}.png"
                await page.screenshot(path=str(path), full_page=False)
                result.screenshots.append(path)
            except Exception as exc:  # noqa: BLE001
                log.debug("screenshot failed: %s", exc)

        async with async_playwright() as pw:
            if self.profiles is not None:
                context = await pw.chromium.launch_persistent_context(
                    str(self.profiles.profile_dir(user_id)),
                    headless=not headful,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    viewport={"width": 1440, "height": 900},
                    locale="id-ID",
                )
            else:
                browser = await pw.chromium.launch(
                    headless=not headful, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900}, locale="id-ID"
                )

            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:  # noqa: BLE001
                    result.message = f"Gagal buka {url}: {str(exc)[:160]}"
                    return result

                await asyncio.sleep(2)
                await shot(page, f"discover-{host}-1-open")
                if progress:
                    await progress(20, f"Halaman {host} terbuka.")

                if sign_in:
                    await self._try_sign_in(page, host, progress=progress)
                    await shot(page, f"discover-{host}-2-session")

                keys = await self._collect(page)
                result.keys = keys
                result.ok = True
                result.message = (
                    f"Aku temukan {len(keys)} key di {host}."
                    if keys
                    else f"Tidak ada key yang terlihat di {host}."
                )
                if progress:
                    await progress(80, result.message)
                await shot(page, f"discover-{host}-3-final")
                return result
            finally:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _try_sign_in(self, page, host: str, *, progress=None) -> bool:
        if self.vault is None:
            return False
        try:
            if self.vault.get(host) is None:
                return False
            from app.services.key_harvester import KeyHarvester

            helper = KeyHarvester(
                self.settings,
                profiles=self.profiles,
                outputs_dir=self.outputs,
                vault=self.vault,
            )
            if not await helper._login_wall(page):
                return False
            if progress:
                await progress(30, f"Login sendiri pakai akun tersimpan ({host})...")
            signed = await helper.sign_in_with_saved_account(page, host, progress=progress)
            if signed:
                await asyncio.sleep(4)
            return signed
        except Exception as exc:  # noqa: BLE001
            log.debug("discovery sign-in skipped: %s", exc)
            return False

    async def _collect(self, page) -> list[FoundKey]:
        """Read the page and pull out anything key-shaped.

        Values in inputs and textareas count, not just visible text: a freshly
        created key is almost always sitting in a readonly input, and a scan of
        rendered text alone would miss exactly the case that matters.
        """
        from app.services.browser_session import host_from_url

        host = host_from_url(page.url)
        found: list[FoundKey] = []
        seen: set[str] = set()

        async def add(kind: str, value: str, source: str) -> None:
            if value in seen:
                return
            seen.add(value)
            found.append(FoundKey(value=value, kind=kind, host=host, source=source))

        # Field values first - that is where a created key lives.
        for selector in (
            "input[type=text]",
            "input[readonly]",
            "input",
            "textarea",
            "code",
            "pre",
        ):
            try:
                values = await page.eval_on_selector_all(
                    selector,
                    "els => els.map(e => e.value || e.textContent || '')",
                )
            except Exception:  # noqa: BLE001
                continue
            for value in values:
                hit = looks_like_key(value)
                if hit:
                    await add(hit[0], hit[1], selector)

        try:
            body = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            body = ""
        for kind, value in scan_text_for_keys(body):
            await add(kind, value, "text")

        return found


def build_discovery(settings, *, profiles=None, vault=None, live_view=None, outputs_dir: Path):
    return KeyDiscovery(
        settings,
        profiles=profiles,
        vault=vault,
        live_view=live_view,
        outputs_dir=outputs_dir,
    )
