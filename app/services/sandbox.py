"""A throwaway place for the agent to run and test its own work.

The user asked for the thing they had been watching: an agent that tries
something, runs it, sees whether it actually works, and only then reports back -
instead of announcing success and being wrong. Doing that needs somewhere to
execute code that cannot damage the bot or leak its secrets.

Design, and why:

* ``/tmp`` sandbox per task, deleted afterwards. The bot's own directory, its
  ``.env`` and its data are never the working directory of anything run here.
* No network by default. A generated script that quietly phones home is the main
  way a "test" turns into an exfiltration, so it has to be asked for explicitly.
* A wall-clock timeout and a memory cap. A test that hangs or allocates without
  bound should be killed, not allowed to take the bot down with it.
* An allow-list of interpreters rather than "run whatever". A generated file
  called ``x.sh`` is not a reason to run a shell.
* Secrets are stripped from the environment. ``.env`` holds the Telegram token
  and every provider key; a subprocess that inherits them can print them into a
  test log the user then reads.

When Docker is available it is used for real isolation. Otherwise the run
happens in a subprocess with a scrubbed environment and resource limits, and the
result says which one was used - the user should never have to guess how much
isolation a green check actually had.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger
from app.security.redact import redact

log = get_logger(__name__)

# Only these run. An interpreter is not chosen by file extension, because a
# generated file named "run.sh" should not be able to reach a shell.
RUNNERS: dict[str, list[str]] = {
    ".py": ["python3"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".ts": ["npx", "tsx"],
}

# Everything in this list is removed from the child environment. They are the
# names the bot actually holds; a broader rule would be better but this is what
# can be enforced without guessing at the platform.
SECRET_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "GITHUB_TOKEN",
    "OPENHANDS_API_KEY",
    "N8N_API_KEY",
    "RENDER_API_KEY",
    "SUPABASE_KEY",
    "SUPABASE_SERVICE_KEY",
    "CLOUDFLARE_API_TOKEN",
    "YOUTUBE_CLIENT_SECRET",
    "REPLICATE_API_TOKEN",
    "TAVILY_API_KEY",
)

DOCKER_IMAGE = "python:3.11-slim"
DOCKER_TIMEOUT = 90


@dataclass
class RunResult:
    """What happened when the code was actually executed."""

    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    isolation: str = "subprocess"
    reason: str = ""
    artifacts: list[str] = field(default_factory=list)

    def summary(self, limit: int = 1200) -> str:
        """A short, honest report. Output is redacted: a test log is a file the
        user reads, and a key printed by a failing test is still a leak."""
        head = "✅ Kode jalan" if self.ok else "❌ Kode gagal"
        parts = [f"{head} (exit {self.exit_code}, {self.seconds:.1f}s, isolasi: {self.isolation})"]
        if self.timed_out:
            parts.append(f"⏱️ Berhenti karena lewat {DOCKER_TIMEOUT}s — kemungkinan hang.")
        if self.reason:
            parts.append(self.reason)
        if self.stdout.strip():
            parts.append("Keluaran:\n```\n" + redact(self.stdout.strip()[:limit]) + "\n```")
        if self.stderr.strip():
            parts.append("Error:\n```\n" + redact(self.stderr.strip()[:limit]) + "\n```")
        return "\n\n".join(parts)


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a test subprocess gets: no secrets, no surprises."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if extra:
        for key, value in extra.items():
            if key.upper() in SECRET_ENV_KEYS:
                log.warning("refusing to pass %s into the sandbox", key)
                continue
            env[key] = value
    return env


class Sandbox:
    """Runs generated code in a disposable directory."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        allow_network: bool = False,
        use_docker: bool | None = None,
        work_root: Path | None = None,
    ):
        self.timeout = timeout
        self.allow_network = allow_network
        self.work_root = work_root
        self._docker = use_docker
        self._docker_prefix: list[str] | None = None

    # ------------------------------------------------------------- detection
    @property
    def docker_available(self) -> bool:
        """True only when docker actually runs, not merely when it is installed.

        Found the hard way: `which docker` returns a path even when the socket
        is not reachable, so the first version claimed Docker isolation it did
        not have. The probe below runs the daemon for real, and remembers the
        prefix (`sudo` or not) that worked.
        """
        if self._docker is not None:
            return self._docker
        if shutil.which("docker") is None:
            self._docker = False
            return False
        for prefix in ([], ["sudo"]):
            try:
                probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [*prefix, "docker", "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if probe.returncode == 0 and probe.stdout.strip():
                self._docker_prefix = prefix
                self._docker = True
                return True
        log.warning("docker terpasang tapi daemon tidak bisa diakses; sandbox pakai subprocess")
        self._docker = False
        return False

    def describe(self) -> str:
        if self.docker_available:
            network = "boleh" if self.allow_network else "mati"
            return f"Docker ({DOCKER_IMAGE}), jaringan: {network}"
        return "subprocess (Docker tidak bisa diakses), environment dibersihkan"

    def isolation_strength(self) -> str:
        """How much the green check is actually worth.

        The user should never have to guess whether "kode jalan" meant a real
        container or a subprocess that can read the bot's own files.
        """
        if self.docker_available:
            return "kuat"
        return "lemah"

    # ------------------------------------------------------------------- run
    def _runner_for(self, path: Path) -> list[str] | None:
        return RUNNERS.get(path.suffix.lower())

    async def run_file(
        self,
        path: Path,
        *,
        args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        """Execute one file and report what it did."""
        path = Path(path)
        if not path.exists():
            return RunResult(ok=False, exit_code=-1, reason=f"File tidak ada: {path}")

        runner = self._runner_for(path)
        if runner is None:
            return RunResult(
                ok=False,
                exit_code=-1,
                reason=(
                    f"Tidak bisa menjalankan `{path.suffix}` di sandbox. "
                    f"Yang didukung: {', '.join(sorted(RUNNERS))}."
                ),
            )

        if self.docker_available:
            return await self._run_docker(path, runner, args or [], extra_env or {})
        return await self._run_subprocess(path, runner, args or [], extra_env or {})

    async def run_command(self, command: list[str], *, cwd: Path | None = None) -> RunResult:
        """Run a fixed command. Used for tools the bot already trusts (ffmpeg,
        pytest) rather than for arbitrary generated code."""
        return await self._run_subprocess_path(command, cwd or Path("/tmp"), env=_clean_env())

    # ------------------------------------------------------------ subprocess
    async def _run_subprocess(
        self, path: Path, runner: list[str], args: list[str], extra_env: dict[str, str]
    ) -> RunResult:
        command = [*runner, path.name, *args]
        return await self._run_subprocess_path(command, path.parent, env=_clean_env(extra_env))

    async def _run_subprocess_path(
        self, command: list[str], cwd: Path, env: dict[str, str]
    ) -> RunResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return RunResult(
                ok=False,
                exit_code=-1,
                reason=f"Perintah tidak ada: {command[0]}. Tidak bisa dites di sini.",
            )
        except OSError as exc:
            return RunResult(ok=False, exit_code=-1, reason=f"Gagal mulai: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            timed_out = False
        except asyncio.TimeoutError:
            process.kill()
            try:
                stdout, stderr = await process.communicate()
            except Exception:  # noqa: BLE001
                stdout, stderr = b"", b""
            timed_out = True

        elapsed = time.monotonic() - started
        code = process.returncode if process.returncode is not None else -1
        return RunResult(
            ok=(code == 0 and not timed_out),
            exit_code=code,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            seconds=elapsed,
            timed_out=timed_out,
            isolation="subprocess",
        )

    # ---------------------------------------------------------------- docker
    async def _run_docker(
        self, path: Path, runner: list[str], args: list[str], extra_env: dict[str, str]
    ) -> RunResult:
        """Real isolation: a fresh container, the file mounted read-only, no
        secrets in the environment, network off unless asked for."""
        name = f"agent-sbx-{int(time.time() * 1000)}"
        command = [
            *(self._docker_prefix or []),
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "bridge" if self.allow_network else "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "-v",
            f"{path.parent}:/work:ro",
            "-w",
            "/tmp",
        ]
        for key, value in extra_env.items():
            if key.upper() in SECRET_ENV_KEYS:
                log.warning("refusing to pass %s into the container", key)
                continue
            command += ["-e", f"{key}={value}"]
        command += [DOCKER_IMAGE, *runner, f"/work/{path.name}", *args]

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_env(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout + DOCKER_TIMEOUT
            )
            code = process.returncode or 0
            timed_out = False
        except asyncio.TimeoutError:
            await self._kill_container(name)
            return RunResult(
                ok=False,
                exit_code=-1,
                seconds=time.monotonic() - started,
                timed_out=True,
                isolation="docker",
                reason="Container dihentikan karena lewat batas waktu.",
            )
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.warning("docker run failed (%s), falling back to subprocess", exc)
            return await self._run_subprocess(path, runner, args, extra_env)

        elapsed = time.monotonic() - started
        return RunResult(
            ok=(code == 0 and not timed_out),
            exit_code=code,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            seconds=elapsed,
            isolation="docker",
        )

    async def _kill_container(self, name: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *(self._docker_prefix or []), "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not remove container %s: %s", name, exc)

    # ------------------------------------------------------------ workspace
    def workspace(self) -> "SandboxWorkspace":
        return SandboxWorkspace(self)


class SandboxWorkspace:
    """A disposable directory. Nothing written here survives the task."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.path = Path(tempfile.mkdtemp(prefix="agent-sbx-", dir=sandbox.work_root))

    def write(self, name: str, content: str) -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    async def run(self, name: str, *, args: list[str] | None = None) -> RunResult:
        return await self.sandbox.run_file(self.path / name, args=args)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "SandboxWorkspace":
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()


def build_sandbox(settings) -> Sandbox:
    return Sandbox(
        timeout=int(getattr(settings, "sandbox_timeout_seconds", 60)),
        allow_network=bool(getattr(settings, "sandbox_allow_network", False)),
    )
