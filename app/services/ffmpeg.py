"""FFmpeg discovery and process helpers.

ffmpeg may come from the system, or from the ``imageio-ffmpeg`` wheel when the
host has no system package. Resolving it once here keeps every media module free
of platform-specific guesswork.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import re
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

_LOCAL_BIN_DIRS = (
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/opt/ffmpeg/bin"),
)


def _find_local_bin(name: str) -> Path | None:
    for directory in _LOCAL_BIN_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def resolve_ffmpeg(settings=None) -> FFmpegPaths:
    """Locate ffmpeg/ffprobe: explicit config -> PATH -> local bin -> imageio wheel."""
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

    # A static build dropped in ~/.local/bin is invisible to a bot started
    # without an interactive shell, so look there before giving up.
    local = _find_local_bin("ffmpeg")
    if local:
        _resolved = FFmpegPaths(str(local), str(_find_local_bin("ffprobe") or local), "local-bin")
        return _resolved

    try:
        import imageio_ffmpeg  # type: ignore

        binary = imageio_ffmpeg.get_ffmpeg_exe()
        # The wheel ships one binary. It answers ffprobe's flags, so probing uses
        # the same path - ``probe_media`` relies on that rather than declaring
        # probing unavailable, which would fail every artifact as unverifiable.
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
    """Return ffprobe-style JSON for *path*, or ``{}`` when probing fails.

    The ``imageio-ffmpeg`` wheel ships only an ``ffmpeg`` binary, no
    ``ffprobe``. Falling back to the ffmpeg binary is fine - it understands the
    ffprobe flags - but two details have to be right or the call silently
    returns nothing, which is how every video once failed QA with "tidak bisa
    diverifikasi":

    * ``-v quiet`` suppresses the JSON on stdout as well as the banner, so the
      output came back empty. ``-v error`` keeps the JSON.
    * ``-show_format``/``-show_streams`` still work, and ``-of json`` is the
      spelling ffmpeg accepts for ``-print_format json``.
    """
    paths = resolve_ffmpeg(settings)
    binary = paths.ffprobe or paths.ffmpeg
    if not binary:
        return {}
    command = [
        binary,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        stdout = await _run(command, timeout=60, collect_stdout=True)
        data = json.loads(stdout or "{}")
        if isinstance(data, dict) and data.get("streams"):
            return data
    except Exception as exc:  # noqa: BLE001
        log.debug("json probe unavailable (%s), falling back to stderr parse", exc)

    # A build without ffprobe (or an ffmpeg that rejects the json flags) still
    # prints everything needed to *verify* a file to stderr. Rather than failing
    # every artifact as unverifiable, reconstruct the fields QA reads.
    fallback = await _probe_via_stderr(binary, path)
    if fallback:
        return fallback
    log.warning("could not probe %s with either method", path)
    return {}


async def _probe_via_stderr(binary: str, path: str | Path) -> dict:
    """Build a minimal ffprobe-shaped dict from ``ffmpeg -i`` output."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "-hide_banner", "-i", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except Exception as exc:  # noqa: BLE001
        log.debug("stderr probe failed: %s", exc)
        return {}

    text = (stderr or b"").decode("utf-8", "ignore")
    duration = 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    streams: list[dict] = []
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        kind = "audio" if "Audio:" in line else "video" if "Video:" in line else ""
        if not kind:
            continue
        stream: dict = {"codec_type": kind}
        size = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if size and kind == "video":
            stream["width"] = int(size.group(1))
            stream["height"] = int(size.group(2))
        streams.append(stream)

    if not streams:
        return {}
    return {
        "streams": streams,
        "format": {"duration": str(duration), "format_name": Path(str(path)).suffix.lstrip(".")},
    }


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
