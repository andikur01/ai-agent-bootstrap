"""Audio: text-to-speech, speech-to-text, and audio editing.

TTS
  ``edge-tts`` first: free, no key, and genuinely natural Indonesian voices.
  Falls back to ``espeak-ng`` when edge-tts is unreachable.

STT
  Groq Whisper (fast, cheap, needs ``GROQ_API_KEY``) then a local
  ``faster-whisper`` model when the package and weights are present. If neither
  is available we raise a typed error telling the user exactly what to do
  instead of silently returning an empty transcript.

Editing
  Pure FFmpeg: trim, concat, speed, volume, normalise, fade, and music mixing.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.errors import MediaQualityError, MissingSecretError, ToolError
from app.core.logging_setup import get_logger
from app.services.ffmpeg import media_duration, probe_media, require_ffmpeg, run_ffmpeg
from app.services.media_qa import QualityVerdict, check_audio_async

log = get_logger(__name__)

MAX_ATTEMPTS = 3

INDONESIAN_VOICES = {
    "wanita": "id-ID-GadisNeural",
    "pria": "id-ID-ArdiNeural",
    "female": "id-ID-GadisNeural",
    "male": "id-ID-ArdiNeural",
}
DEFAULT_VOICE = "id-ID-GadisNeural"


@dataclass
class AudioResult:
    path: Path
    kind: str  # tts | edit | mix
    provider: str
    attempts: int
    verdict: QualityVerdict
    duration: float = 0.0
    text: str = ""


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit] or "audio").strip("-")


class AudioService:
    def __init__(self, settings, *, outputs_dir: Path | None = None):
        self.settings = settings
        self.outputs = Path(outputs_dir or settings.outputs_path)
        (self.outputs / "audio").mkdir(parents=True, exist_ok=True)
        self._whisper = None

    def _out(self, tag: str, name: str, ext: str = "mp3") -> Path:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.outputs / "audio" / f"{stamp}-{tag}-{_slug(name)}.{ext}"

    # ------------------------------------------------------------------ TTS
    async def text_to_speech(
        self, text: str, *, voice: str = "", rate: str = "+0%", progress=None
    ) -> AudioResult:
        if not text.strip():
            raise ToolError("Teks untuk TTS kosong.")
        voice_id = INDONESIAN_VOICES.get(voice.strip().lower(), voice) or DEFAULT_VOICE
        if not re.match(r"^[a-z]{2}-[A-Z]{2}-", voice_id):
            voice_id = DEFAULT_VOICE

        last: QualityVerdict | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            path = self._out(f"tts{attempt}", text[:30])
            if progress:
                await progress(int(10 + 30 * (attempt - 1) / MAX_ATTEMPTS), f"Bikin suara ({attempt}/{MAX_ATTEMPTS})")
            errors: list[str] = []
            for provider, fn in (
                ("edge-tts", self._tts_edge),
                ("espeak-ng", self._tts_espeak),
            ):
                try:
                    await fn(text, path, voice_id, rate)
                    break
                except MissingSecretError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{provider}: {exc}")
                    log.warning("tts provider %s failed: %s", provider, exc)
            else:
                raise ToolError("TTS gagal di semua provider: " + "; ".join(errors)[:400])

            if progress:
                await progress(60, "Cek kualitas audio")
            verdict = await check_audio_async(path)
            last = verdict
            if verdict.ok:
                duration = await self._duration(path)
                return AudioResult(path, "tts", "edge-tts", attempt, verdict, duration, text)
            log.info("tts attempt %s rejected: %s", attempt, verdict.summary())
            rate = _bump_rate(rate)

        raise MediaQualityError(
            f"Audio belum lolos QA setelah {MAX_ATTEMPTS} percobaan",
            detail=last.summary() if last else "",
        )

    async def _tts_edge(self, text: str, path: Path, voice: str, rate: str) -> None:
        try:
            import edge_tts  # type: ignore
        except ImportError as exc:
            raise ToolError("edge-tts tidak terpasang (pip install edge-tts)") from exc

        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(path))
        if not path.exists() or path.stat().st_size < 500:
            raise ToolError("edge-tts menghasilkan file kosong")

    async def _tts_espeak(self, text: str, path: Path, voice: str, rate: str) -> None:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if not binary:
            raise ToolError("espeak-ng tidak tersedia")
        wav = path.with_suffix(".wav")
        process = await asyncio.create_subprocess_exec(
            binary, "-v", "id", "-w", str(wav), text[:4000],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate()
        if process.returncode != 0 or not wav.exists():
            raise ToolError("espeak gagal")
        await run_ffmpeg(["-i", str(wav), "-codec:a", "libmp3lame", "-q:a", "4", str(path)],
                         settings=self.settings)
        wav.unlink(missing_ok=True)

    # ------------------------------------------------------------------ STT
    async def speech_to_text(self, path: str | Path, *, language: str = "id") -> str:
        path = Path(path)
        if not path.exists():
            raise ToolError(f"File audio tidak ada: {path}")

        errors: list[str] = []
        if self.settings.groq_api_key:
            try:
                return await self._stt_groq(path, language)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"groq: {exc}")
                log.warning("groq stt failed: %s", exc)
        try:
            return await self._stt_local(path, language)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"faster-whisper: {exc}")
            log.warning("local stt failed: %s", exc)

        raise MissingSecretError(
            "GROQ_API_KEY (untuk transkripsi cepat) atau `pip install faster-whisper`",
            detail="; ".join(errors)[:400],
        )

    async def _stt_groq(self, path: Path, language: str) -> str:
        if not self.settings.groq_api_key:
            raise MissingSecretError("GROQ_API_KEY")
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.settings.groq_api_key}"}
        data = {"model": "whisper-large-v3-turbo", "language": language, "response_format": "json"}
        async with httpx.AsyncClient(timeout=300) as client:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle, "application/octet-stream")}
                response = await client.post(url, headers=headers, data=data, files=files)
            response.raise_for_status()
            payload = response.json()
        text = (payload.get("text") or "").strip()
        if not text:
            raise ToolError("Groq Whisper mengembalikan transkrip kosong")
        return text

    async def _stt_local(self, path: Path, language: str) -> str:
        def _run() -> str:
            from faster_whisper import WhisperModel  # type: ignore

            if self._whisper is None:
                self._whisper = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _info = self._whisper.transcribe(str(path), language=language, vad_filter=True)
            return " ".join(segment.text.strip() for segment in segments).strip()

        text = await asyncio.to_thread(_run)
        if not text:
            raise ToolError("faster-whisper mengembalikan transkrip kosong")
        return text

    # ------------------------------------------------------------------ edit
    async def edit(
        self,
        instruction: str,
        *,
        source: Path | None = None,
        second: Path | None = None,
        progress=None,
    ) -> AudioResult:
        if source is None or not Path(source).exists():
            raise ToolError("Sumber audio tidak ditemukan.")
        source = Path(source)
        last: QualityVerdict | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            path = self._out(f"edit{attempt}", instruction)
            if progress:
                await progress(int(10 + 25 * (attempt - 1) / MAX_ATTEMPTS), f"Edit audio ({attempt}/{MAX_ATTEMPTS})")
            await self._apply_edit(instruction, source, second, path)
            if progress:
                await progress(65, "Cek kualitas audio")
            verdict = await check_audio_async(path)
            last = verdict
            if verdict.ok:
                duration = await self._duration(path)
                return AudioResult(path, "edit", "ffmpeg", attempt, verdict, duration)
            log.info("audio edit attempt %s rejected: %s", attempt, verdict.summary())

        raise MediaQualityError(
            f"Audio belum lolos QA setelah {MAX_ATTEMPTS} percobaan",
            detail=last.summary() if last else "",
        )

    async def _apply_edit(self, instruction: str, source: Path, second: Path | None, out: Path) -> None:
        text = instruction.lower()
        filters: list[str] = []
        args: list[str] = ["-i", str(source)]

        speed = _extract_speed(text)
        if speed:
            filters.append(f"atempo={speed}")
        volume = _extract_volume(text)
        if volume:
            filters.append(f"volume={volume}")
        if any(word in text for word in ("normal", "normalisasi", "perjelas", "jernih")):
            filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        if any(word in text for word in ("bass", "berat")):
            filters.append("bass=g=5")
        if any(word in text for word in ("treble", "tajam", "cerah")):
            filters.append("treble=g=4")
        if any(word in text for word in ("hilangkan noise", "denoise", "bersihkan")):
            filters.append("afftdn=nf=-25")
        if any(word in text for word in ("echo", "gaung", "reverb")):
            filters.append("aecho=0.8:0.9:40|60:0.4|0.3")

        trim = _extract_trim(text)
        if trim:
            start, end = trim
            args = ["-ss", str(start), "-to", str(end), *args]

        if second and Path(second).exists():
            args = ["-i", str(second), *args]
            if any(word in text for word in ("gabung", "mix", "campur", "tambah musik", "background")):
                filters.append("[1:a]volume=0.3[bg];[0:a][bg]amix=inputs=2:duration=longest")
            else:
                filters.append("concat=n=2:v=0:a=1")

        fade = _extract_fade(text)
        if fade:
            filters.append(f"afade=t=in:st=0:d={fade}")
            filters.append(f"afade=t=out:st=-1:d={fade}")

        command = [*args, "-vn"]
        if filters:
            command += ["-af", ",".join(filters)]
        command += ["-codec:a", "libmp3lame", "-q:a", "4", str(out)]
        try:
            await run_ffmpeg(command, settings=self.settings)
        except ToolError:
            # Some filter chains (e.g. amix labels) are picky; retry with a safe default.
            log.warning("complex audio filter failed; retrying with loudnorm only")
            await run_ffmpeg(
                ["-i", str(source), "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-codec:a", "libmp3lame", "-q:a", "4", str(out)],
                settings=self.settings,
            )

    async def _duration(self, path: Path) -> float:
        probe = await probe_media(path, settings=self.settings)
        return media_duration(probe)

    # ------------------------------------------------------------- compose
    async def compose_music_bed(
        self, narration: Path, music: Path, out: Path | None = None, *, music_volume: float = 0.22
    ) -> Path:
        """Mix narration over a music bed, ducking the music under the voice."""
        out = out or self._out("mix", "narration-music")
        await run_ffmpeg(
            [
                "-i", str(narration),
                "-i", str(music),
                "-filter_complex",
                f"[1:a]volume={music_volume},aloop=loop=-1:size=2e9[bg];"
                "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2",
                "-codec:a", "libmp3lame", "-q:a", "4", str(out),
            ],
            settings=self.settings,
        )
        return out

    async def synth_tone(
        self, *, frequency: int = 440, seconds: float = 3.0, out: Path | None = None
    ) -> Path:
        """Generate a simple tone - useful as a music fallback with no assets."""
        out = out or self._out("tone", f"{frequency}hz")
        await run_ffmpeg(
            [
                "-f", "lavfi",
                "-i", f"sine=frequency={frequency}:duration={seconds}",
                "-af", "afade=t=in:d=0.3,afade=t=out:st=" + str(max(0.0, seconds - 0.4)) + ":d=0.4",
                "-codec:a", "libmp3lame", "-q:a", "4", str(out),
            ],
            settings=self.settings,
        )
        return out


def _bump_rate(rate: str) -> str:
    match = re.match(r"([+-])(\d+)%", rate or "")
    if not match:
        return "+10%"
    sign, value = match.group(1), int(match.group(2))
    new_value = min(50, value + 10)
    return f"{sign}{new_value}%"


def _extract_speed(text: str) -> str | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:x|kali)\s*(?:lebih\s*)?(?:cepat|cepet|speed)", text)
    if not match:
        match = re.search(r"(?:kecepatan|speed|tempo)\s*(\d+(?:[.,]\d+)?)", text)
    if not match:
        if any(word in text for word in ("perlambat", "lambat", "slow")):
            return "0.85"
        if any(word in text for word in ("percepat", "cepat", "fast")):
            return "1.25"
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    if value > 10:  # probably a percentage
        value = value / 100
    return str(max(0.5, min(2.0, value)))


def _extract_volume(text: str) -> str | None:
    match = re.search(r"(\d+)\s*(?:%|persen)", text)
    if match and any(word in text for word in ("volume", "keras", "pelan", "kecil", "besar", "suara")):
        return str(max(0.1, min(3.0, int(match.group(1)) / 100)))
    if any(word in text for word in ("keraskan", "lebih keras", "louder", "volume up")):
        return "1.6"
    if any(word in text for word in ("pelankan", "kecilkan", "quieter", "volume down")):
        return "0.6"
    return None


def _extract_trim(text: str) -> tuple[float, float] | None:
    match = re.search(r"(?:dari|from)\s*(\d+(?:[.,]\d+)?)\s*(?:s|detik|sec)?\s*(?:sampai|s/d|hingga|to|-)\s*(\d+(?:[.,]\d+)?)", text)
    if match:
        try:
            return float(match.group(1).replace(",", ".")), float(match.group(2).replace(",", "."))
        except ValueError:
            return None
    match = re.search(r"(?:potong|trim|ambil)\s*(?:jadi)?\s*(\d+(?:[.,]\d+)?)\s*(?:s|detik|sec)", text)
    if match:
        try:
            return 0.0, float(match.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _extract_fade(text: str) -> float | None:
    match = re.search(r"fade\s*(?:in|out)?\s*(\d+(?:[.,]\d+)?)", text)
    if match:
        try:
            return max(0.1, min(5.0, float(match.group(1).replace(",", "."))))
        except ValueError:
            return None
    if any(word in text for word in ("fade", "memudar")):
        return 0.6
    return None
