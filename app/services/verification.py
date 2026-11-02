"""Test the work before claiming it is done.

This is the behaviour the user asked to be built in: an agent that tries
something, runs it, checks the result, fixes what is broken, and only then
reports back - rather than announcing success and being wrong.

Two ideas, deliberately separate:

* **Verification** runs the artifact through the right checker (Playwright for a
  site, ffprobe for a video, the sandbox for a script) and returns evidence.
* **The gate** decides what to do with a failure. It retries up to a limit,
  then reports the failure honestly instead of shipping it with a cheerful
  message. A checker that could not run at all is *not* a pass - it is an
  explicit "not verified", because a skipped test that reads as a green tick is
  worse than no test.

Every result carries the evidence: what was run, what it found, and how many
attempts it took. The user should be able to see the check, not just the claim.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.core.logging_setup import get_logger
from app.security.redact import redact

log = get_logger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class Attempt:
    """One try: what was done, and what checking it found."""

    index: int
    ok: bool
    evidence: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    skipped: bool = False
    note: str = ""

    def line(self) -> str:
        if self.skipped:
            return f"Percobaan {self.index}: ⚠️ tidak bisa diverifikasi — {self.note}"
        mark = "✅" if self.ok else "❌"
        detail = "; ".join(self.problems[:3]) if self.problems else "bersih"
        return f"Percobaan {self.index}: {mark} {detail}"


@dataclass
class Verification:
    """The outcome of the whole try/check/fix cycle."""

    ok: bool
    attempts: list[Attempt] = field(default_factory=list)
    skipped: bool = False
    summary: str = ""
    evidence: list[str] = field(default_factory=list)

    def report(self) -> str:
        """Plain-language report for Telegram. Honest about which of the three
        states it ended in: verified, fixed, or still broken."""
        if self.skipped:
            return f"⚠️ *Belum diverifikasi.*\n\n{self.summary}"
        head = "✅ *Sudah aku tes sendiri, dan jalan.*"
        if not self.ok:
            head = "❌ *Masih ada masalah setelah aku coba perbaiki.*"
        lines = [head, ""]
        for attempt in self.attempts:
            lines.append("• " + attempt.line())
        if self.summary:
            lines += ["", self.summary]
        return "\n".join(lines)


async def verify_with_retries(
    *,
    produce: Callable[[int], Awaitable[Path | None]] | None = None,
    check: Callable[[Path | None], Awaitable[tuple[bool, list[str]]]],
    fix: Callable[[int, list[str]], Awaitable[Path | None]] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> Verification:
    """Produce, check, fix, repeat - then report the truth.

    ``check`` returns ``(ok, problems)``. It raises ``VerificationSkipped`` when
    the checker itself is unavailable, which is reported as unverified rather
    than as a pass.
    """
    attempts: list[Attempt] = []
    artifact: Path | None = None

    for index in range(1, max_attempts + 1):
        if index > 1 and fix is not None:
            if progress:
                await progress(f"🔧 Perbaikan ke-{index - 1} dari {max_attempts - 1}…")
            artifact = await fix(index, attempts[-1].problems)
        elif produce is not None:
            if progress:
                await progress(f"⚙️ Mengerjakan (percobaan {index})…")
            artifact = await produce(index)

        try:
            ok, problems = await check(artifact)
        except VerificationSkipped as exc:
            attempts.append(Attempt(index=index, ok=False, skipped=True, note=str(exc)))
            return Verification(
                ok=False,
                attempts=attempts,
                skipped=True,
                summary=(
                    f"Aku tidak bisa mengetesnya otomatis: {exc}\n"
                    "Jadi aku *tidak* bilang ini sudah bagus. Tolong lihat sendiri dulu."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a broken checker is not a pass
            log.warning("checker error: %s", exc)
            attempts.append(
                Attempt(index=index, ok=False, skipped=True, note=f"pemeriksa error: {exc}")
            )
            return Verification(
                ok=False,
                attempts=attempts,
                skipped=True,
                summary=f"Alat pemeriksanya error, jadi hasilnya belum aku jamin: {exc}",
            )

        attempts.append(Attempt(index=index, ok=ok, problems=problems))
        if ok:
            break

    final = attempts[-1]
    if final.ok:
        summary = "Sudah lolos pemeriksaan otomatis."
        if len(attempts) > 1:
            summary = f"Lolos setelah {len(attempts)} percobaan (aku perbaiki sendiri)."
    else:
        summary = (
            "Aku sudah coba {} kali dan masih ada yang gagal:\n".format(len(attempts))
            + "\n".join(f"- {redact(p)}" for p in final.problems[:5])
        )
    return Verification(ok=final.ok, attempts=attempts, summary=summary)


class VerificationSkipped(Exception):
    """The checker could not run. Never treat this as success."""


# --------------------------------------------------------------- checkers


async def check_script_in_sandbox(sandbox, path: Path) -> tuple[bool, list[str]]:
    """Run a generated script and see whether it actually works."""
    if sandbox is None:
        raise VerificationSkipped("sandbox tidak tersedia")
    result = await sandbox.run_file(path)
    if result.ok:
        return True, []
    problems = [f"exit {result.exit_code}"]
    if result.timed_out:
        problems.append("program hang, dihentikan")
    if result.stderr.strip():
        problems.append(redact(result.stderr.strip().splitlines()[-1])[:200])
    return False, problems


async def check_media(path: Path, *, kind: str = "auto") -> tuple[bool, list[str]]:
    """Confirm a generated image/video/audio is real, not blank or silent."""
    from app.services import media_qa

    suffix = Path(path).suffix.lower()
    if kind == "auto":
        if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            kind = "video"
        elif suffix in {".mp3", ".wav", ".ogg", ".m4a", ".opus"}:
            kind = "audio"
        else:
            kind = "image"

    checker = {
        "video": media_qa.check_video_async,
        "audio": media_qa.check_audio_async,
        "image": media_qa.check_image_async,
    }[kind]
    verdict = await checker(path)
    if verdict.ok:
        return True, []
    return False, list(verdict.reasons) or [f"hasil {kind} tidak lolos pemeriksaan"]


async def check_site(qa, url: str, *, retries: int = 1) -> tuple[bool, list[str]]:
    """Load a deployed site in a real browser and look for breakage."""
    if qa is None:
        raise VerificationSkipped("browser QA tidak tersedia")
    try:
        from app.services.browser_qa import browser_ready
    except Exception as exc:  # noqa: BLE001
        raise VerificationSkipped(f"modul browser QA tidak bisa dimuat: {exc}") from exc
    if not browser_ready():
        raise VerificationSkipped("Playwright/browser belum terpasang di server ini")

    report = await qa.check_site(url)
    problems = list(report.issues)
    return report.ok, problems
