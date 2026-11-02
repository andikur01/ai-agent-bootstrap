"""The agent must test its own work before saying it is done.

The user's request was blunt: stop announcing success and being wrong. These
tests hold the two halves of that promise in place - the sandbox that runs the
work, and the gate that refuses to call an unverified result a pass.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.sandbox import SECRET_ENV_KEYS, Sandbox, _clean_env
from app.services.verification import (
    VerificationSkipped,
    check_media,
    check_script_in_sandbox,
    verify_with_retries,
)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------- sandbox


def test_generated_code_that_works_is_reported_as_working(tmp_path):
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("ok.py", "print('halo'); print(2 + 3)")
        result = run(ws.run("ok.py"))

    assert result.ok
    assert "halo" in result.stdout
    assert "5" in result.stdout


def test_generated_code_that_crashes_is_reported_as_failing(tmp_path):
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("bad.py", "raise ValueError('sengaja')")
        result = run(ws.run("bad.py"))

    assert not result.ok
    assert "sengaja" in result.stderr


def test_a_hanging_script_is_killed_not_waited_for(tmp_path):
    """A test that never returns would otherwise hang the whole bot."""
    sandbox = Sandbox(timeout=3, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("hang.py", "import time; time.sleep(600)")
        result = run(ws.run("hang.py"))

    assert result.timed_out
    assert not result.ok
    assert result.seconds < 30


def test_secrets_are_stripped_from_the_sandbox_environment(monkeypatch):
    """A subprocess that inherits .env can print the Telegram token into a test
    log the user then reads. The names are removed before the child starts."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "RAHASIA_JANGAN_BOCOR")
    env = _clean_env()

    assert "TELEGRAM_BOT_TOKEN" not in env
    for key in SECRET_ENV_KEYS:
        assert key not in env


def test_a_script_cannot_read_the_bot_token_from_its_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "RAHASIA_JANGAN_BOCOR")
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("spy.py", "import os; print(os.environ.get('TELEGRAM_BOT_TOKEN'))")
        result = run(ws.run("spy.py"))

    assert "RAHASIA_JANGAN_BOCOR" not in result.stdout
    assert "None" in result.stdout


def test_requested_extra_secrets_are_refused_not_passed():
    """Asking to pass a secret in must not be a way around the rule."""
    env = _clean_env({"TELEGRAM_BOT_TOKEN": "RAHASIA", "SAFE_VAR": "ok"})

    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["SAFE_VAR"] == "ok"


def test_an_unsupported_file_type_is_refused_rather_than_guessed():
    """A generated file named x.sh must not reach a shell just because it ends
    in .sh."""
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("evil.sh", "echo pwned")
        result = run(ws.run("evil.sh"))

    assert not result.ok
    assert "didukung" in result.reason.lower()


def test_the_workspace_is_deleted_afterwards():
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("x.py", "print(1)")
        path = ws.path
        assert path.exists()

    assert not path.exists(), "a temp workspace must not be left behind"


def test_the_isolation_strength_is_reported_honestly():
    """The user should never have to guess whether a green check came from a
    real container or a subprocess that can read the bot's own files."""
    sandbox = Sandbox(use_docker=False)
    assert sandbox.isolation_strength() == "lemah"

    sandbox = Sandbox(use_docker=True)
    assert sandbox.isolation_strength() == "kuat"


# ------------------------------------------------------------------ verification


def _check_factory(sandbox, path):
    async def check(_artifact):
        return await check_script_in_sandbox(sandbox, path)

    return check


def test_a_working_artifact_passes_on_the_first_attempt():
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        ws.write("ok.py", "print('jalan')")
        verdict = run(
            verify_with_retries(check=_check_factory(sandbox, ws.path / "ok.py"))
        )

    assert verdict.ok
    assert len(verdict.attempts) == 1
    assert "Sudah aku tes sendiri" in verdict.report()


def test_a_broken_artifact_is_fixed_and_then_passes():
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        target = ws.path / "work.py"

        async def produce(_index):
            target.write_text("raise ValueError('bug')")
            return target

        async def fix(_index, _problems):
            target.write_text("print('diperbaiki')")
            return target

        verdict = run(
            verify_with_retries(
                produce=produce, check=_check_factory(sandbox, target), fix=fix
            )
        )

    assert verdict.ok
    assert len(verdict.attempts) == 2
    assert "aku perbaiki sendiri" in verdict.summary


def test_an_unfixable_artifact_is_reported_as_broken_not_as_done():
    """The whole point: never claim success that is not there."""
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        target = ws.path / "stuck.py"
        target.write_text("raise RuntimeError('tidak bisa')")

        async def produce(_index):
            return target

        async def fix(_index, _problems):
            return target

        verdict = run(
            verify_with_retries(
                produce=produce,
                check=_check_factory(sandbox, target),
                fix=fix,
                max_attempts=3,
            )
        )

    assert not verdict.ok
    assert len(verdict.attempts) == 3
    assert "Masih ada masalah" in verdict.report()


def test_a_checker_that_cannot_run_is_not_a_pass():
    """A skipped test that reads as a green tick is worse than no test."""

    async def unavailable(_artifact):
        raise VerificationSkipped("browser tidak terpasang")

    async def produce(_index):
        return None

    verdict = run(verify_with_retries(produce=produce, check=unavailable))

    assert not verdict.ok
    assert verdict.skipped
    assert "Belum diverifikasi" in verdict.report()


def test_a_checker_that_itself_errors_is_not_a_pass():
    async def broken(_artifact):
        raise RuntimeError("pemeriksa rusak")

    async def produce(_index):
        return None

    verdict = run(verify_with_retries(produce=produce, check=broken))

    assert not verdict.ok
    assert verdict.skipped


def test_retry_count_is_capped():
    """Burning tokens in a loop helps nobody."""
    sandbox = Sandbox(timeout=30, use_docker=False)
    with sandbox.workspace() as ws:
        target = ws.path / "always_bad.py"
        target.write_text("raise SystemExit(1)")

        async def produce(_index):
            return target

        async def fix(_index, _problems):
            return target

        verdict = run(
            verify_with_retries(
                produce=produce,
                check=_check_factory(sandbox, target),
                fix=fix,
                max_attempts=2,
            )
        )

    assert len(verdict.attempts) == 2


def test_a_missing_file_is_reported_rather_than_crashing():
    sandbox = Sandbox(timeout=30, use_docker=False)
    result = run(sandbox.run_file(Path("/tmp/definitely-not-here-12345.py")))

    assert not result.ok
    assert "tidak ada" in result.reason.lower()


def test_a_blank_image_fails_the_media_check(tmp_path):
    """A solid-colour PNG is the classic "generation failed but returned a
    file" case."""
    from PIL import Image

    path = tmp_path / "blank.png"
    Image.new("RGB", (512, 512), (255, 255, 255)).save(path)

    ok, problems = run(check_media(path, kind="image"))

    assert not ok
    assert problems


def test_every_task_reports_a_single_token_number():
    """A real crash, found in the live log rather than in a test.

    Outcome.tokens was set from TOKEN_ESTIMATES, which holds a (low, high)
    pair. Formatting it as a number killed the whole task with
    "unsupported format string passed to tuple.__format__" - the user saw
    nothing at all for their website request. Every intent must produce one
    integer.
    """
    from app.core.orchestrator import TOKEN_ESTIMATES, estimate_tokens_for
    from app.core.intent import Intent

    for intent in TOKEN_ESTIMATES:
        value = estimate_tokens_for(intent)
        assert isinstance(value, int), f"{intent} harus satu angka, bukan {type(value)}"
        assert value > 0
        # This is the exact call that used to raise.
        assert f"{value:,}"


def test_the_token_estimate_stays_inside_its_documented_range():
    from app.core.orchestrator import TOKEN_ESTIMATES, estimate_tokens_for
    from app.core.intent import Intent

    for intent, (low, high) in TOKEN_ESTIMATES.items():
        assert low <= estimate_tokens_for(intent) <= high


def test_handlers_only_use_attributes_that_exist():
    """A real AttributeError, caught by running /tes rather than by a test.

    The command read `self.orchestrator`, but the handler stores it as
    `self.orch`, so /tes crashed on first use. Guarding the exact mistake keeps
    it from coming back.
    """
    from pathlib import Path

    source = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    assert "self.orchestrator" not in source, "atributnya bernama self.orch"


def test_the_sandbox_is_reachable_from_the_handler():
    """The command must be able to find the sandbox, or /tes is a dead button."""
    from pathlib import Path

    source = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    assert 'getattr(self.orch, "sandbox"' in source
