"""Probing must work even where only the imageio ffmpeg binary exists.

Every video once failed QA with "tidak bisa diverifikasi (ffprobe tidak
mengembalikan data)". The cause was not a bad video. The ``imageio-ffmpeg``
wheel ships a single binary, and it rejects ffprobe's JSON flags outright
("Unrecognized option 'print_format'"), so probing returned nothing and the
quality gate failed every artifact - a silent, total failure.

These tests pin the recovery: a real JSON probe when ffprobe exists, and a
stderr-based reconstruction when it does not.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.services.ffmpeg import media_duration, probe_media, resolve_ffmpeg


def _make_clip(tmp_path: Path) -> Path:
    """A real two-second clip, so the probe has something true to read."""
    paths = resolve_ffmpeg()
    if not paths.ffmpeg:
        pytest.skip("no ffmpeg available")
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            paths.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
            "-pix_fmt", "yuv420p", str(out),
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    return out


def test_probe_reads_a_real_file(tmp_path):
    clip = _make_clip(tmp_path)
    probe = asyncio.run(probe_media(clip))
    assert probe, "a real video must be probeable"
    streams = probe.get("streams") or []
    kinds = {s.get("codec_type") for s in streams}
    assert "video" in kinds, f"expected a video stream, got {kinds}"
    assert media_duration(probe) > 1.0, "duration must be read"


def test_the_stderr_fallback_also_works(tmp_path):
    """Even with no JSON support, the probe must return usable data."""
    from app.services.ffmpeg import _probe_via_stderr

    clip = _make_clip(tmp_path)
    probe = asyncio.run(_probe_via_stderr(resolve_ffmpeg().ffmpeg, clip))
    assert probe, "the fallback must not come back empty"
    assert probe["streams"][0]["codec_type"] == "video"
    assert float(probe["format"]["duration"]) > 1.0


def test_a_missing_file_does_not_raise(tmp_path):
    probe = asyncio.run(probe_media(tmp_path / "nope.mp4"))
    assert probe == {}, "an unreadable file is reported as no data, not a crash"
