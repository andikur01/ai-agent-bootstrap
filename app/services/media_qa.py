"""Automatic quality checks for generated media.

The point is to catch garbage *before* it reaches the user: blank images, silent
audio, zero-frame videos, files that are obviously too small to be real. Each
check returns a structured verdict plus human-readable reasons, so the calling
module can decide whether to retry (max 3 attempts) or ship.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class QualityVerdict:
    ok: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return f"kualitas OK (skor {self.score:.2f})"
        return f"kualitas kurang (skor {self.score:.2f}): " + "; ".join(self.reasons)


def check_image(path: str | Path, *, min_side: int = 256, min_bytes: int = 4000) -> QualityVerdict:
    """Reject blank, tiny, or near-uniform images."""
    path = Path(path)
    reasons: list[str] = []
    metrics: dict = {}
    if not path.exists():
        return QualityVerdict(False, 0.0, ["file tidak ada"])

    size = path.stat().st_size
    metrics["bytes"] = size
    if size < min_bytes:
        reasons.append(f"file terlalu kecil ({size} B)")

    try:
        from PIL import Image, ImageStat
    except ImportError:
        return QualityVerdict(not reasons, 0.6 if not reasons else 0.0, reasons, metrics)

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            width, height = img.size
            metrics["width"], metrics["height"] = width, height
            if min(width, height) < min_side:
                reasons.append(f"resolusi rendah ({width}x{height})")

            stat = ImageStat.Stat(img)
            means = stat.mean
            metrics["mean"] = [round(m, 1) for m in means]
            spread = max(means) - min(means)

            gray = img.convert("L")
            hist = gray.histogram()
            total = sum(hist) or 1
            entropy = 0.0
            for count in hist:
                if count:
                    p = count / total
                    entropy -= p * math.log2(p)
            metrics["entropy"] = round(entropy, 2)

            # Near-uniform image: single flat colour or a washed-out frame.
            if entropy < 2.0 and spread < 6:
                reasons.append("gambar hampir polos / kosong")

            # All-black or all-white
            avg = sum(means) / 3
            if avg < 8:
                reasons.append("gambar nyaris hitam total")
            elif avg > 247:
                reasons.append("gambar nyaris putih total")

            # Sharpness proxy: variance of the Laplacian on a downscaled grey copy.
            small = gray.resize((min(width, 512), min(height, 512)))
            pixels = list(small.getdata())
            w, h = small.size
            lap_sum = 0.0
            lap_sq = 0.0
            n = 0
            for y in range(1, h - 1):
                row = y * w
                for x in range(1, w - 1):
                    idx = row + x
                    lap = (
                        4 * pixels[idx]
                        - pixels[idx - 1]
                        - pixels[idx + 1]
                        - pixels[idx - w]
                        - pixels[idx + w]
                    )
                    lap_sum += lap
                    lap_sq += lap * lap
                    n += 1
            if n:
                mean_lap = lap_sum / n
                variance = lap_sq / n - mean_lap * mean_lap
                metrics["sharpness"] = round(variance, 1)
                if variance < 8:
                    reasons.append("gambar terlalu blur / tanpa detail")
    except Exception as exc:  # noqa: BLE001
        return QualityVerdict(False, 0.0, [f"gagal membuka gambar: {exc}"], metrics)

    score = _score(len(reasons), metrics)
    return QualityVerdict(not reasons, score, reasons, metrics)


def check_audio(path: str | Path, *, min_seconds: float = 0.6, min_bytes: int = 2000) -> QualityVerdict:
    """Reject silent or truncated audio using an RMS scan over decoded samples."""
    path = Path(path)
    if not path.exists():
        return QualityVerdict(False, 0.0, ["file tidak ada"])

    reasons: list[str] = []
    metrics: dict = {"bytes": path.stat().st_size}
    if metrics["bytes"] < min_bytes:
        reasons.append(f"file terlalu kecil ({metrics['bytes']} B)")

    try:
        import subprocess

        from app.services.ffmpeg import require_ffmpeg

        ffmpeg = require_ffmpeg().ffmpeg
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        stderr = proc.stderr or ""
        for line in stderr.splitlines():
            if "mean_volume" in line:
                metrics["mean_volume_db"] = float(line.split(":")[-1].strip().split()[0])
            elif "max_volume" in line:
                metrics["max_volume_db"] = float(line.split(":")[-1].strip().split()[0])
        mean_db = metrics.get("mean_volume_db", 0.0)
        max_db = metrics.get("max_volume_db", 0.0)
        if max_db < -50 or mean_db < -60:
            reasons.append(f"audio nyaris senyap (mean {mean_db} dB)")
        if mean_db > -6:
            reasons.append(f"audio kemungkinan clipping (mean {mean_db} dB)")
    except Exception as exc:  # noqa: BLE001
        log.warning("audio quality probe failed: %s", exc)
        metrics["probe_error"] = str(exc)[:200]

    try:
        from app.services.ffmpeg import media_duration, probe_media

        import asyncio

        probe = asyncio.run(probe_media(path)) if not _in_loop() else {}
        duration = media_duration(probe)
        if duration:
            metrics["duration"] = round(duration, 2)
            if duration < min_seconds:
                reasons.append(f"audio terlalu pendek ({duration:.1f}s)")
    except Exception:  # noqa: BLE001
        pass

    return QualityVerdict(not reasons, _score(len(reasons), metrics), reasons, metrics)


def check_video(path: str | Path, *, min_seconds: float = 1.0, min_bytes: int = 20000) -> QualityVerdict:
    """Reject videos with no video stream, no frames, or a broken container."""
    path = Path(path)
    if not path.exists():
        return QualityVerdict(False, 0.0, ["file tidak ada"])

    reasons: list[str] = []
    metrics: dict = {"bytes": path.stat().st_size}
    if metrics["bytes"] < min_bytes:
        reasons.append(f"file terlalu kecil ({metrics['bytes']} B)")

    try:
        from app.services.ffmpeg import media_duration, probe_media

        if _in_loop():
            probe: dict = {}
        else:
            import asyncio

            probe = asyncio.run(probe_media(path))
        if probe:
            streams = probe.get("streams", [])
            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            metrics["video_streams"] = len(video_streams)
            metrics["audio_streams"] = len(audio_streams)
            duration = media_duration(probe)
            metrics["duration"] = round(duration, 2)
            if not video_streams:
                reasons.append("tidak ada stream video")
            if duration and duration < min_seconds:
                reasons.append(f"video terlalu pendek ({duration:.1f}s)")
            if video_streams:
                frames = video_streams[0].get("nb_frames")
                if frames and str(frames).isdigit() and int(frames) < 2:
                    reasons.append("jumlah frame tidak wajar")
        else:
            metrics["probe_error"] = "ffprobe kosong"
    except Exception as exc:  # noqa: BLE001
        log.warning("video quality probe failed: %s", exc)
        metrics["probe_error"] = str(exc)[:200]

    return QualityVerdict(not reasons, _score(len(reasons), metrics), reasons, metrics)


def check_html(path: str | Path, *, min_bytes: int = 800) -> QualityVerdict:
    """Static sanity for generated HTML: real content, no lorem ipsum, has title."""
    path = Path(path)
    if not path.exists():
        return QualityVerdict(False, 0.0, ["file tidak ada"])
    text = path.read_text(encoding="utf-8", errors="ignore")
    reasons: list[str] = []
    metrics = {"bytes": len(text.encode("utf-8"))}
    lowered = text.lower()
    if metrics["bytes"] < min_bytes:
        reasons.append("HTML terlalu kecil / kosong")
    if "lorem ipsum" in lowered or "lorem-ipsum" in lowered:
        reasons.append("masih ada lorem ipsum (dilarang)")
    if "<title" not in lowered:
        reasons.append("tidak ada <title> (SEO dasar)")
    if "viewport" not in lowered:
        reasons.append("tidak ada meta viewport (responsif)")
    if "<html" not in lowered:
        reasons.append("bukan dokumen HTML")
    return QualityVerdict(not reasons, _score(len(reasons), metrics), reasons, metrics)


def _score(reason_count: int, metrics: dict) -> float:
    base = 1.0 - 0.22 * reason_count
    return max(0.0, min(1.0, base))


def _in_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def check_image_async(path) -> QualityVerdict:
    import asyncio

    return await asyncio.to_thread(check_image, path)


async def check_audio_async(path) -> QualityVerdict:
    import asyncio

    return await asyncio.to_thread(check_audio, path)


async def check_video_async(path) -> QualityVerdict:
    import asyncio

    return await asyncio.to_thread(check_video, path)


async def check_html_async(path) -> QualityVerdict:
    import asyncio

    return await asyncio.to_thread(check_html, path)
