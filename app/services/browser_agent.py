"""A browser the agent drives itself, one step at a time.

``BrowserQA`` answers one question: does this page load cleanly? This module
answers a harder one: can the agent *use* the page - find a field, type, click,
and read what happened. It powers "buka situs ini, login, ambil datanya".

The loop is deliberately literal. The model sees a numbered list of interactive
elements and replies with one action; we execute it, observe the result, and ask
again. That keeps every step auditable and lets a single bad guess be corrected
instead of derailing a whole script.

Safety: every click and type targets a numbered element from the last
observation, never a free-form selector, and the run stops on the step budget or
on a completion signal. Nothing here submits a payment or creates an account on
its own - it only does what the model asks for inside the page it was given.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger(__name__)

MAX_ELEMENTS = 40
DEFAULT_STEPS = 8

# Elements worth exposing to the model: things a person can actually operate.
_INTERACTIVE = (
    "a[href]",
    "button",
    "input:not([type=hidden])",
    "textarea",
    "select",
    "[role=button]",
    "[role=link]",
    "[contenteditable=true]",
)


@dataclass
class Element:
    index: int
    tag: str
    label: str
    kind: str = ""
    name: str = ""

    def render(self) -> str:
        bits = [f"[{self.index}]", self.tag]
        if self.kind:
            bits.append(f"({self.kind})")
        if self.name:
            bits.append(f"name={self.name}")
        bits.append(f'"{self.label}"')
        return " ".join(bits)


@dataclass
class Observation:
    url: str
    title: str
    text: str
    elements: list[Element] = field(default_factory=list)
    screenshot: str = ""
    error: str = ""

    def render(self) -> str:
        lines = [f"URL: {self.url}", f"Judul: {self.title}", "", "Elemen:"]
        lines += [f"  {e.render()}" for e in self.elements] or ["  (tidak ada)"]
        body = self.text.strip()
        if body:
            lines += ["", "Teks halaman:", body[:1500]]
        return "\n".join(lines)


@dataclass
class Action:
    kind: str  # click | type | goto | scroll | back | wait | download | done
    index: int = 0
    text: str = ""
    url: str = ""
    reason: str = ""
    answer: str = ""


@dataclass
class BrowserRun:
    ok: bool
    steps: list[str] = field(default_factory=list)
    answer: str = ""
    screenshots: list[str] = field(default_factory=list)
    error: str = ""


_ACTION_SCHEMA = """Balas HANYA JSON, satu aksi per langkah:
{"action":"click","index":3,"reason":"buka menu login"}
{"action":"click","index":4,"reason":"tunggu hasil render"}
{"action":"type","index":2,"text":"halo","reason":"isi pencarian"}
{"action":"download","index":7,"reason":"unduh hasil video/gambar"}
{"action":"goto","url":"https://...","reason":"pindah halaman"}
{"action":"scroll","reason":"lihat bawah halaman"}
{"action":"back","reason":"kembali"}
{"action":"wait","reason":"tunggu muat"}
{"action":"done","answer":"jawaban akhir untuk user","reason":"selesai"}

Aturan:
- index harus salah satu nomor elemen yang terlihat di observasi terakhir.
- Pakai "done" begitu tugas selesai atau kalau sudah buntu.
- Jangan mengarang elemen yang tidak ada."""


def parse_action(payload: Any) -> Action:
    """Turn a model reply into an Action, tolerating prose around the JSON."""
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return Action("done", answer=text[:500], reason="jawaban bukan JSON")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return Action("done", answer=text[:500], reason="JSON tidak valid")
    if not isinstance(payload, dict):
        return Action("done", reason="format tidak dikenali")

    kind = str(payload.get("action") or payload.get("kind") or "done").strip().lower()
    if kind not in {"click", "type", "goto", "scroll", "back", "wait", "download", "done"}:
        kind = "done"
    index = payload.get("index", 0)
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    return Action(
        kind=kind,
        index=index,
        text=str(payload.get("text") or ""),
        url=str(payload.get("url") or ""),
        reason=str(payload.get("reason") or ""),
        answer=str(payload.get("answer") or "") if kind == "done" else "",
    )


class BrowserAgent:
    """Drives a real Chromium page under the model's direction."""

    def __init__(
        self,
        settings,
        *,
        gateway,
        outputs_dir: Path,
        max_steps: int | None = None,
        profiles=None,
        vault=None,
        live_view=None,
    ):
        self.settings = settings
        self.gateway = gateway
        self.outputs = Path(outputs_dir)
        (self.outputs / "browser").mkdir(parents=True, exist_ok=True)
        self.max_steps = max_steps or int(getattr(settings, "browser_max_steps", DEFAULT_STEPS))
        # The saved session and the shared display. Without them this agent can
        # only read public pages: it cannot see anything behind a login, and the
        # user cannot watch or take over when a captcha appears.
        self.profiles = profiles
        self.vault = vault
        self.live_view = live_view
        # Set when the agent downloads a produced file, so the caller can send it.
        self.downloaded: Path | None = None

    # ------------------------------------------------------------------ run
    async def run(
        self,
        url: str,
        task: str,
        *,
        user_id: int = 0,
        progress=None,
        max_steps: int | None = None,
    ) -> BrowserRun:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return BrowserRun(False, error="Playwright belum terpasang.")

        budget = max_steps or self.max_steps
        run = BrowserRun(ok=False)
        await self._prepare_display()
        async with async_playwright() as pw:
            context = await self._context(pw, user_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:  # noqa: BLE001
                    return BrowserRun(False, error=f"Gagal buka {url}: {str(exc)[:200]}")

                # A saved login turns a wall into a page the agent can work with.
                await self._maybe_sign_in(page, url, progress=progress)

                history = [f"Tugas: {task}"]
                for step in range(1, budget + 1):
                    observation = await self._observe(page, step)
                    run.screenshots.append(observation.screenshot)

                    if progress:
                        await progress(
                            10 + int(80 * step / budget),
                            f"Langkah {step}/{budget}: {observation.title or observation.url}",
                            screenshot=observation.screenshot,
                        )

                    action = await self._decide(observation, history, user_id)
                    run.steps.append(
                        f"{step}. {action.kind}"
                        + (f" #{action.index}" if action.index else "")
                        + (f" {action.text!r}" if action.text else "")
                        + (f" -> {action.reason}" if action.reason else "")
                    )

                    if action.kind == "done":
                        run.ok = True
                        run.answer = action.answer or "Selesai."
                        break

                    outcome = await self._apply(page, action, observation)
                    history.append(f"Aksi: {action.kind} {action.index} -> {outcome}")
                    if len(history) > 12:  # keep the prompt small
                        history = history[:1] + history[-8:]
                else:
                    run.ok = False
                    run.answer = "Batas langkah tercapai sebelum tugas selesai."

                final = await self._observe(page, budget + 1)
                run.screenshots.append(final.screenshot)
                if run.ok and not run.answer.strip():
                    run.answer = final.text[:1500]
            finally:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
        return run

    # -------------------------------------------------------------- internals
    async def _prepare_display(self) -> None:
        """Point DISPLAY at the shared screen, before Playwright starts.

        Playwright's driver reads the environment once and hands it to every
        browser it spawns, so this has to happen before ``async_playwright()``.
        Doing it means the user can watch the agent work, and - more importantly
        - can take over the same screen when a site asks for a captcha.
        """
        if self.live_view is None:
            return
        try:
            state = await self.live_view.ensure()
        except Exception as exc:  # noqa: BLE001
            log.debug("no shared display for the browser agent: %s", exc)
            return
        if state.running and state.display:
            import os

            os.environ["DISPLAY"] = state.display

    async def _context(self, pw, user_id: int):
        """Open the browser with the user's saved session, if there is one.

        A fresh incognito context is fine for reading a public page, but it is
        useless for anything behind a login - the agent would hit the wall and
        have nothing to sign in with. Reusing the profile keeps the session the
        user established once, and headful keeps it on the shared display.
        """
        headful = False
        if self.live_view is not None:
            try:
                state = await self.live_view.ensure()
                headful = bool(state.running)
            except Exception:  # noqa: BLE001
                headful = False

        if self.profiles is not None:
            profile = self.profiles.profile_dir(user_id)
            return await pw.chromium.launch_persistent_context(
                str(profile),
                headless=not headful,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                viewport={"width": 1440, "height": 900},
                locale="id-ID",
            )

        browser = await pw.chromium.launch(
            headless=not headful, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        return await browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="id-ID"
        )

    async def _maybe_sign_in(self, page, url: str, *, progress=None) -> bool:
        """Sign in with a saved account when the page is a login wall.

        This is what makes 'buka akun aku dan kerjakan ini' possible: the agent
        reads the site, notices it is asking for a login, and answers with the
        account the user saved instead of stopping.
        """
        if self.vault is None:
            return False
        try:
            from app.services.browser_session import host_from_url

            host = host_from_url(url)
        except Exception:  # noqa: BLE001
            return False
        if self.vault.get(host) is None:
            return False

        try:
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
                await progress(10, f"Login sendiri pakai akun tersimpan ({host})...")
            return await helper.sign_in_with_saved_account(page, host, progress=progress)
        except Exception as exc:  # noqa: BLE001
            log.debug("saved-account sign-in skipped: %s", exc)
            return False

    async def _observe(self, page, step: int) -> Observation:
        try:
            url = page.url
            title = (await page.title())[:200]
        except Exception as exc:  # noqa: BLE001
            return Observation(url="", title="", text="", error=str(exc)[:200])

        elements: list[Element] = []
        try:
            handles = await page.query_selector_all(", ".join(_INTERACTIVE))
            for handle in handles[:MAX_ELEMENTS]:
                try:
                    if not await handle.is_visible():
                        continue
                    info = await handle.evaluate(
                        """el => ({
                            tag: el.tagName.toLowerCase(),
                            type: el.getAttribute('type') || '',
                            name: el.getAttribute('name') || '',
                            text: (el.innerText || el.value || el.getAttribute('aria-label')
                                   || el.getAttribute('placeholder') || el.getAttribute('title')
                                   || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
                            href: el.getAttribute('href') || ''
                        })"""
                    )
                except Exception:  # noqa: BLE001
                    continue

                label = info.get("text") or info.get("href") or ""
                if not label:
                    continue

                index = len(elements) + 1
                # Stamp the element so a later action can find this exact node
                # again. A Playwright handle does not survive a reload, and
                # matching on visible text picks the wrong node when labels
                # repeat (three "Edit" links, say).
                try:
                    await handle.evaluate(
                        "(el, i) => el.setAttribute('data-agent-idx', i)", index
                    )
                except Exception:  # noqa: BLE001
                    continue

                elements.append(
                    Element(
                        index=index,
                        tag=info.get("tag", "?"),
                        label=label[:80],
                        kind=info.get("type", ""),
                        name=info.get("name", "")[:30],
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("element scan failed: %s", exc)

        try:
            text = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""

        shot = self.outputs / "browser" / f"step-{step:02d}.png"
        try:
            await page.screenshot(path=str(shot), full_page=False)
        except Exception:  # noqa: BLE001
            shot = None  # type: ignore[assignment]

        return Observation(
            url=url,
            title=title,
            text=text[:4000],
            elements=elements,
            screenshot=str(shot) if shot else "",
        )

    async def _decide(self, observation: Observation, history: list[str], user_id: int) -> Action:
        messages = [
            {"role": "system", "content": _ACTION_SCHEMA},
            {
                "role": "user",
                "content": (
                    "Riwayat:\n"
                    + "\n".join(history[-6:])
                    + "\n\nObservasi sekarang:\n"
                    + observation.render()
                    + "\n\nAksi berikutnya?"
                ),
            },
        ]
        try:
            result = await self.gateway.complete(
                messages, tier="small", user_id=user_id, temperature=0.1, json_mode=True
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("browser decision failed: %s", exc)
            return Action("done", answer="", reason=f"model gagal: {str(exc)[:120]}")
        return parse_action(result.text)

    async def _apply(self, page, action: Action, observation: Observation) -> str:
        """Execute one action and describe what happened, for the next prompt."""
        try:
            if action.kind == "goto" and action.url:
                await page.goto(action.url, wait_until="domcontentloaded", timeout=30000)
                return f"pindah ke {action.url}"
            if action.kind == "back":
                await page.go_back(timeout=15000)
                return "kembali"
            if action.kind == "wait":
                await page.wait_for_timeout(1500)
                return "menunggu"
            if action.kind == "scroll":
                await page.mouse.wheel(0, 900)
                await page.wait_for_timeout(600)
                return "scroll ke bawah"

            target = next((e for e in observation.elements if e.index == action.index), None)
            if target is None:
                return f"elemen #{action.index} tidak ada"

            handle = await self._locator(page, target)
            if handle is None:
                return f"elemen #{action.index} sudah tidak ada di halaman"

            if action.kind == "click":
                await handle.click(timeout=8000)
                await page.wait_for_timeout(1200)
                return f"klik {target.label!r}"
            if action.kind == "type":
                await handle.fill(action.text, timeout=8000)
                return f"isi {target.label!r} dengan {action.text!r}"
            if action.kind == "download":
                return await self._download(page, handle, target)
            return f"aksi {action.kind} tidak dikenal"
        except Exception as exc:  # noqa: BLE001
            return f"gagal: {str(exc)[:160]}"

    async def _download(self, page, handle, target: Element) -> str:
        """Click something that produces a file, and keep the file.

        A tool site - the kind that makes video or images - ends by handing the
        user a download. Without this the agent can reach the final page and then
        have nothing: the result would be a filename in a browser it cannot see
        afterwards. Anything it produces goes to outputs/browser/downloads so it
        can be sent on to Telegram.
        """
        target_dir = self.outputs / "browser" / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with page.expect_download(timeout=60000) as info:
                await handle.click(timeout=8000)
            download = await info.value
            suggested = download.suggested_filename or "unduhan"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", suggested)[:80] or "unduhan"
            destination = target_dir / safe
            await download.save_as(str(destination))
            size = destination.stat().st_size if destination.exists() else 0
            if size == 0:
                return f"unduhan {safe} kosong"
            self.downloaded = destination
            return f"terunduh {safe} ({size} bytes) ke {destination}"
        except Exception as exc:  # noqa: BLE001
            # Many sites open the file in a new tab instead of downloading it.
            # Fall back to reading the URL the click produced.
            try:
                async with page.context.expect_page(timeout=15000) as info:
                    await handle.click(timeout=8000)
                new_page = await info.value
                await new_page.wait_for_load_state("domcontentloaded", timeout=20000)
                return f"terbuka tab baru: {new_page.url}"
            except Exception:  # noqa: BLE001
                return f"gagal mengunduh: {str(exc)[:120]}"

    async def _locator(self, page, target: Element):
        """Re-find the element by the tag we stamped during the last scan."""
        try:
            handle = await page.query_selector(f'[data-agent-idx="{target.index}"]')
            if handle:
                return handle
        except Exception as exc:  # noqa: BLE001
            log.debug("relocate failed for #%s: %s", target.index, exc)
        return None


def build_browser_agent(settings, *, gateway, outputs_dir: Path) -> BrowserAgent:
    return BrowserAgent(settings, gateway=gateway, outputs_dir=outputs_dir)
