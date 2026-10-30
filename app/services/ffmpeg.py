"""FFmpeg discovery and process helpers.

ffmpeg may come from the system, or from the ``imageio-ffmpeg`` wheel when the
host has no system package. Resolving it once here keeps every media module free
of platform-specific guesswork.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class FFmpegPaths:
    ffmpeg: str
    ffprobe: str
    source: str

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)


_resolved: FFmpegPaths | None = None


def resolve_ffmpeg(settings=None) -> FFmpegPaths:
    """Locate ffmpeg/ffprobe: explicit config -> PATH -> imageio-ffmpeg wheel."""
    global _resolved
    if _resolved is not None:
        return _resolved

    if settings is not None:
        explicit = shutil.which(settings.ffmpeg_bin)
        if explicit:
            probe = shutil.which(settings.ffprobe_bin) or explicit
            _resolved = FFmpegPaths(explicit, probe, "config")
            return _resolved

    system = shutil.which("ffmpeg")
    if system:
        _resolved = FFmpegPaths(system, shutil.which("ffprobe") or system, "system")
        return _resolved

    try:
        import imageio_ffmpeg  # type: ignore

        binary = imageio_ffmpeg.get_ffmpeg_exe()
        _resolved = FFmpegPaths(binary, binary, "imageio-ffmpeg")
        return _resolved
    except Exception as exc:  # noqa: BLE001
        log.warning("ffmpeg not found: %s", exc)
        _resolved = FFmpegPaths("", "", "missing")
        return _resolved


def require_ffmpeg(settings=None) -> FFmpegPaths:
    paths = resolve_ffmpeg(settings)
    if not paths.ffmpeg:
        raise ToolError(
            "FFmpeg tidak tersedia. Install `ffmpeg` di sistem atau `pip install imageio-ffmpeg`."
        )
    return paths


async def run_ffmpeg(args: list[str], *, timeout: int = 900, settings=None) -> str:
    """Run ffmpeg with *args* (without the leading binary). Returns stderr tail."""
    paths = require_ffmpeg(settings)
    command = [paths.ffmpeg, "-hide_banner", "-nostdin", "-y", *args]
    return await _run(command, timeout=timeout)


async def probe_media(path: str | Path, *, settings=None) -> dict:
    """Return ffprobe JSON for *path*, or ``{}`` when probing fails."""
    paths = resolve_ffmpeg(settings)
    binary = paths.ffprobe or paths.ffmpeg
    if not binary:
        return {}
    command = [
        binary,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        stdout = await _run(command, timeout=60, collect_stdout=True)
        return json.loads(stdout or "{}")
    except Exception as exc:  # noqa: BLE001
        log.warning("ffprobe failed for %s: %s", path, exc)
        return {}


async def _run(command: list[str], *, timeout: int = 900, collect_stdout: bool = False) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ToolError(f"Perintah timeout setelah {timeout}s: {command[0]}") from exc

    if process.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "ignore")[-800:]
        raise ToolError(f"{Path(command[0]).name} gagal (exit {process.returncode}): {tail}")
    if collect_stdout:
        return (stdout or b"").decode("utf-8", "ignore")
    return (stderr or b"").decode("utf-8", "ignore")[-400:]


def media_duration(probe: dict) -> float:
    try:
        return float(probe.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        return 0.0


def media_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0
