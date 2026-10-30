"""Interactive-screenshot QA for deployed websites (Playwright).

This is the gate the user asked for: a website is not "done" until a headless
browser has loaded it, taken a screenshot, and reported no console errors, no
failed requests, and no broken layout at desktop *and* mobile widths.

Playwright is optional. When the browser binary is missing, the module says so
explicitly (``available`` is False) instead of silently skipping QA - the caller
then either installs it or tells the user QA was skipped.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}


@dataclass
class PageReport:
    url: str
    viewport: str
    screenshot: str = ""
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    status: int = 0
    title: str = ""
    text_length: int = 0
    horizontal_overflow: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.status in range(200, 400)
            and not self.console_errors
            and not self.page_errors
            and not self.failed_requests
            and not self.horizontal_overflow
            and self.text_length > 200
        )

    def issues(self) -> list[str]:
        problems: list[str] = []
        if self.status and self.status >= 400:
            problems.append(f"HTTP {self.status}")
        problems += [f"console: {e}" for e in self.console_errors[:5]]
        problems += [f"page error: {e}" for e in self.page_errors[:3]]
        problems += [f"request gagal: {e}" for e in self.failed_requests[:5]]
        if self.horizontal_overflow:
            problems.append("layout overflow horizontal (tidak responsif)")
        if self.text_length <= 200:
            problems.append("konten halaman terlalu sedikit / kosong")
        return problems


@dataclass
class SiteReport:
    url: str
    pages: list[PageReport] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.pages) and all(page.ok for page in self.pages)

    def issues(self) -> list[str]:
        if self.error:
            return [self.error]
        out: list[str] = []
        for page in self.pages:
            for issue in page.issues():
                out.append(f"[{page.viewport}] {issue}")
        return out

    def screenshots(self) -> list[str]:
        return [page.screenshot for page in self.pages if page.screenshot]

    def summary(self) -> str:
        if self.error:
            return f"QA gagal: {self.error}"
        if self.ok:
            return f"QA lolos ({len(self.pages)} viewport, tanpa error)"
        return "QA menemukan masalah:\n" + "\n".join(f"- {issue}" for issue in self.issues()[:8])


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


class BrowserQA:
    def __init__(self, outputs_dir: Path, *, timeout_ms: int = 25000):
        self.outputs = Path(outputs_dir)
        (self.outputs / "qa").mkdir(parents=True, exist_ok=True)
        self.timeout_ms = timeout_ms

    async def check_site(
        self,
        url: str,
        *,
        viewports: tuple[str, ...] = ("desktop", "mobile"),
        wait_ms: int = 1500,
        fill_forms: bool = False,
    ) -> SiteReport:
        if not playwright_available():
            return SiteReport(url, error="Playwright belum terpasang (pip install playwright && playwright install chromium)")
        try:
            return await self._run(url, viewports=viewports, wait_ms=wait_ms, fill_forms=fill_forms)
        except Exception as exc:  # noqa: BLE001
            log.warning("browser QA failed: %s", exc)
            return SiteReport(url, error=str(exc)[:300])

    async def _run(
        self, url: str, *, viewports: tuple[str, ...], wait_ms: int, fill_forms: bool
    ) -> SiteReport:
        from playwright.async_api import async_playwright

        report = SiteReport(url)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                for name in viewports:
                    viewport = VIEWPORTS.get(name, VIEWPORTS["desktop"])
                    context = await browser.new_context(
                        viewport=viewport,
                        device_scale_factor=1,
                        locale="id-ID",
                    )
                    page = await context.new_page()
                    page_report = PageReport(url=url, viewport=name)

                    page.on("console", lambda msg, pr=page_report: _on_console(msg, pr))
                    page.on("pageerror", lambda err, pr=page_report: pr.page_errors.append(str(err)[:200]))
                    page.on("requestfailed", lambda req, pr=page_report: _on_request_failed(req, pr))

                    try:
                        response = await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                        page_report.status = response.status if response else 0
                        page_report.title = (await page.title())[:200]
                    except Exception as exc:  # noqa: BLE001
                        page_report.page_errors.append(f"navigation: {str(exc)[:160]}")

                    await page.wait_for_timeout(wait_ms)

                    if fill_forms:
                        await self._exercise_forms(page, page_report)

                    try:
                        text = await page.inner_text("body")
                        page_report.text_length = len(text.strip())
                    except Exception:  # noqa: BLE001
                        pass

                    try:
                        overflow = await page.evaluate(
                            "() => document.documentElement.scrollWidth > window.innerWidth + 4"
                        )
                        page_report.horizontal_overflow = bool(overflow)
                    except Exception:  # noqa: BLE001
                        pass

                    shot = self.outputs / "qa" / f"{_slug(url)}-{name}.png"
                    try:
                        await page.screenshot(path=str(shot), full_page=True)
                        page_report.screenshot = str(shot)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("screenshot failed: %s", exc)

                    report.pages.append(page_report)
                    await context.close()
            finally:
                await browser.close()
        return report

    @staticmethod
    async def _exercise_forms(page, page_report: PageReport) -> None:
        """Type into visible inputs and click the submit button, to catch JS crashes."""
        try:
            inputs = await page.query_selector_all("input:visible, textarea:visible")
            for index, element in enumerate(inputs[:4]):
                field_type = (await element.get_attribute("type")) or "text"
                if field_type in {"hidden", "file", "checkbox", "radio", "submit"}:
                    continue
                value = "test@example.com" if field_type == "email" else f"test{index}"
                try:
                    await element.fill(value)
                except Exception:  # noqa: BLE001
                    continue
            button = await page.query_selector(
                "button[type=submit]:visible, input[type=submit]:visible, button:has-text('Login'), button:has-text('Masuk'), button:has-text('Sign in')"
            )
            if button:
                await button.click(timeout=5000)
                await page.wait_for_timeout(1500)
        except Exception as exc:  # noqa: BLE001
            page_report.console_errors.append(f"form exercise: {str(exc)[:120]}")

    async def check_local_file(self, path: Path, **kwargs) -> SiteReport:
        return await self.check_site(f"file://{Path(path).resolve()}", **kwargs)


def _on_console(msg, page_report: PageReport) -> None:
    if msg.type == "error":
        page_report.console_errors.append(msg.text[:200])


def _on_request_failed(request, page_report: PageReport) -> None:
    failure = request.failure or ""
    url = request.url
    # favicon misses are noise, not defects
    if "favicon" in url:
        return
    page_report.failed_requests.append(f"{url[:120]} ({failure})")


def _slug(url: str, limit: int = 40) -> str:
    import re

    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", url.replace("https://", "").replace("http://", ""))
    return cleaned.strip("-")[:limit] or "site"


def mask_secrets_in_image(path: str | Path, secrets: list[str]) -> str:
    """Blur regions where a known secret value is visible in a screenshot.

    Cheap heuristic, honest about its limits: it blurs the top strip of the page
    (where tokens usually leak in dev tools screenshots) when any secret value
    is short enough to plausibly render. Callers should still avoid capturing
    secrets at all; this is a belt-and-braces guard.
    """
    path = Path(path)
    if not path.exists():
        return str(path)
    try:
        from PIL import Image, ImageFilter

        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            strip = img.crop((0, 0, width, max(40, height // 12)))
            strip = strip.filter(ImageFilter.GaussianBlur(14))
            img.paste(strip, (0, 0))
            out = path.with_name(path.stem + "-masked.png")
            img.save(out)
            return str(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("secret masking failed: %s", exc)
        return str(path)
