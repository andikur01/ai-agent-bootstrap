"""Video creation and editing.

Creation has two tracks, chosen by what the user actually asked for:

* ``manim`` - motion graphics. When the request is explainer-ish ("animasi
  rumus pythagoras", "jelaskan cara kerja DNS") we ask the LLM for a small,
  validated scene spec and render it with Manim. The LLM never emits Python
  code that we execute blindly: it emits JSON, and we build the scene from a
  whitelist of scene types.
* ``slideshow`` - the always-available path. Text cards rendered with Pillow,
  composed with FFmpeg, optional narration from the audio service. This works
  with zero GPU, zero keys, and no model downloads.

Editing is pure FFmpeg: trim, concat, speed, scale, subtitle burn-in, music
mix, watermark.

AI video via Replicate is used only when the user explicitly asks for a
generated/live-action clip and ``REPLICATE_API_TOKEN`` is present.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.core.errors import MediaQualityError, MissingSecretError, ToolError
from app.core.logging_setup import get_logger
from app.services.ffmpeg import media_duration, probe_media, run_ffmpeg
from app.services.media_qa import QualityVerdict, check_video_async

log = get_logger(__name__)

MAX_ATTEMPTS = 3

SCENE_SPEC_PROMPT = """Buat spesifikasi animasi edukatif pendek sebagai JSON.

Skema:
{
  "title": "<judul singkat>",
  "scenes": [
    {
      "type": "title|bullets|equation|steps|chart|outro",
      "heading": "<judul scene>",
      "lines": ["<teks baris 1>", "<teks baris 2>"],
      "duration": <detik, 3-8>
    }
  ],
  "narration": "<narasi Bahasa Indonesia 2-4 kalimat untuk voice-over>"
}

Aturan:
- 3 sampai 5 scene.
- Bahasa Indonesia.
- "equation" hanya untuk rumus matematika sederhana (LaTeX tanpa \\begin).
- Konten harus faktual dan spesifik. Jangan placeholder.
Balas JSON saja."""

TITLE_CARD_COLORS = [
    ((15, 23, 42), (248, 250, 252)),
    ((30, 41, 59), (226, 232, 240)),
    ((17, 24, 39), (241, 245, 249)),
    ((2, 44, 34), (236, 253, 245)),
    ((45, 20, 60), (250, 245, 255)),
]


@dataclass
class VideoResult:
    path: Path
    kind: str  # manim | slideshow | edit | ai
    provider: str
    attempts: int
    verdict: QualityVerdict
    duration: float = 0.0
    spec: dict = field(default_factory=dict)
    narration: Path | None = None


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit] or "video").strip("-")


class VideoService:
    def __init__(self, settings, gateway=None, audio=None, *, outputs_dir: Path | None = None):
        self.settings = settings
        self.gateway = gateway
        self.audio = audio
        self.outputs = Path(outputs_dir or settings.outputs_path)
        (self.outputs / "video").mkdir(parents=True, exist_ok=True)

    def _out(self, tag: str, name: str, ext: str = "mp4") -> Path:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.outputs / "video" / f"{stamp}-{tag}-{_slug(name)}.{ext}"

    # --------------------------------------------------------------- create
    async def create(
        self,
        request: str,
        *,
        duration_hint: float = 20.0,
        with_narration: bool = True,
        progress=None,
    ) -> VideoResult:
        spec = await self._plan(request, duration_hint=duration_hint)
        last: QualityVerdict | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if progress:
                await progress(int(10 + 15 * (attempt - 1) / MAX_ATTEMPTS), f"Rencana video siap, render ({attempt}/{MAX_ATTEMPTS})")
            path = self._out(f"video{attempt}", spec.get("title") or request)
            provider = "slideshow"
            errors: list[str] = []

            renderers = []
            if _manim_available() and _is_mathy(request, spec):
                renderers.append(("manim", self._render_manim))
            renderers.append(("slideshow", self._render_slideshow))

            for name, renderer in renderers:
                try:
                    await renderer(spec, path, progress)
                    provider = name
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {exc}")
                    log.warning("video renderer %s failed: %s", name, exc)
            else:
                raise ToolError("Render video gagal: " + "; ".join(errors)[:500])

            narration_path = None
            if with_narration and self.audio and spec.get("narration"):
                try:
                    if progress:
                        await progress(70, "Bikin narasi suara")
                    tts = await self.audio.text_to_speech(spec["narration"])
                    narration_path = tts.path
                    if progress:
                        await progress(80, "Gabungkan narasi + video")
                    path = await self._attach_narration(path, narration_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("narration failed, shipping silent video: %s", exc)

            if progress:
                await progress(88, "Cek kualitas video")
            verdict = await check_video_async(path)
            last = verdict
            if verdict.ok:
                duration = await self._duration(path)
                return VideoResult(path, provider, provider, attempt, verdict, duration, spec, narration_path)
            log.info("video attempt %s rejected: %s", attempt, verdict.summary())

        raise MediaQualityError(
            f"Video belum lolos QA setelah {MAX_ATTEMPTS} percobaan",
            detail=last.summary() if last else "",
        )

    async def _plan(self, request: str, *, duration_hint: float) -> dict:
        """Get a validated scene spec: LLM when possible, template otherwise."""
        if self.gateway and self.gateway.available():
            try:
                data = await self.gateway.complete_json(
                    [
                        {"role": "system", "content": SCENE_SPEC_PROMPT},
                        {
                            "role": "user",
                            "content": f"Topik/permintaan: {request}\nTotal durasi target: sekitar {duration_hint:.0f} detik.",
                        },
                    ],
                    tier="big",
                    temperature=0.3,
                    max_tokens=1200,
                )
                spec = _validate_spec(data, request)
                if spec:
                    return spec
            except Exception as exc:  # noqa: BLE001
                log.warning("video planning failed: %s", exc)
        return _template_spec(request)

    # ------------------------------------------------------- slideshow path
    async def _render_slideshow(self, spec: dict, out: Path, progress=None) -> None:
        await asyncio.to_thread(self._slideshow_sync, spec, out)

    def _slideshow_sync(self, spec: dict, out: Path) -> None:
        from PIL import Image, ImageDraw, ImageFont

        width, height, fps = 1280, 720, 30
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scene_files: list[Path] = []
            for index, scene in enumerate(spec.get("scenes", [])):
                bg, fg = TITLE_CARD_COLORS[index % len(TITLE_CARD_COLORS)]
                image = Image.new("RGB", (width, height), bg)
                draw = ImageDraw.Draw(image)
                title_font = _load_font(46)
                body_font = _load_font(30)
                small_font = _load_font(20)

                heading = str(scene.get("heading") or spec.get("title") or "")
                draw.text((70, 70), heading[:60], font=title_font, fill=fg)
                draw.line([(70, 140), (width - 70, 140)], fill=fg, width=2)

                lines = scene.get("lines") or []
                if scene.get("type") == "equation" and lines:
                    draw.text((width // 2 - 60, height // 2 - 40), str(lines[0])[:60],
                              font=_load_font(54), fill=fg)
                else:
                    y = 190
                    for line in lines[:6]:
                        wrapped = _wrap(str(line), 62)
                        for part in wrapped[:2]:
                            draw.text((90, y), "• " + part if not part.startswith("•") else part,
                                      font=body_font, fill=fg)
                            y += 44
                        y += 6
                draw.text((70, height - 50), f"{index + 1}/{len(spec.get('scenes', []))}  •  AI agent",
                          font=small_font, fill=fg)

                frame = tmp_path / f"frame{index:03d}.png"
                image.save(frame)
                scene_files.append(frame)

            if not scene_files:
                raise ToolError("Tidak ada scene untuk dirender")

            concat = tmp_path / "concat.txt"
            with concat.open("w") as handle:
                for frame, scene in zip(scene_files, spec.get("scenes", [])):
                    duration = float(scene.get("duration") or 5.0)
                    duration = max(2.0, min(12.0, duration))
                    handle.write(f"file '{frame}'\nduration {duration:.2f}\n")
                handle.write(f"file '{scene_files[-1]}'\n")

            run = asyncio.run if not _in_loop() else None
            args = [
                "-f", "concat", "-safe", "0", "-i", str(concat),
                "-vf", f"fps={fps},format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
            ]
            if run:
                run(run_ffmpeg(args, settings=self.settings))
            else:
                _run_ffmpeg_blocking(args, self.settings)

    # ----------------------------------------------------------- manim path
    async def _render_manim(self, spec: dict, out: Path, progress=None) -> None:
        scene_code = _manim_scene_code(spec)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = tmp_path / "scene.py"
            script.write_text(scene_code, encoding="utf-8")
            binary = shutil.which("manim")
            if not binary:
                raise ToolError("manim tidak terpasang")
            process = await asyncio.create_subprocess_exec(
                binary, "-ql", "--media_dir", str(tmp_path / "media"),
                str(script), "AgentScene",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=600)
            if process.returncode != 0:
                raise ToolError("manim gagal: " + (stdout or b"").decode("utf-8", "ignore")[-400:])
            rendered = list((tmp_path / "media").rglob("AgentScene.mp4"))
            if not rendered:
                raise ToolError("manim tidak menghasilkan file")
            shutil.copy2(rendered[0], out)

    # ------------------------------------------------------------ narration
    async def _attach_narration(self, video: Path, narration: Path) -> Path:
        merged = video.with_name(video.stem + "-narrated.mp4")
        await run_ffmpeg(
            [
                "-i", str(video),
                "-i", str(narration),
                "-filter_complex", "[1:a]apad[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                "-shortest", "-movflags", "+faststart", str(merged),
            ],
            settings=self.settings,
        )
        return merged

    # ------------------------------------------------------------------ edit
    async def edit(
        self,
        instruction: str,
        *,
        source: Path | None = None,
        second: Path | None = None,
        subtitle: str = "",
        music: Path | None = None,
        progress=None,
    ) -> VideoResult:
        if source is None or not Path(source).exists():
            raise ToolError("Sumber video tidak ditemukan.")
        source = Path(source)
        last: QualityVerdict | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            path = self._out(f"edit{attempt}", instruction)
            if progress:
                await progress(int(10 + 25 * (attempt - 1) / MAX_ATTEMPTS), f"Edit video ({attempt}/{MAX_ATTEMPTS})")
            await self._apply_edit(instruction, source, second, path, subtitle, music)
            if progress:
                await progress(70, "Cek kualitas video")
            verdict = await check_video_async(path)
            last = verdict
            if verdict.ok:
                duration = await self._duration(path)
                return VideoResult(path, "edit", "ffmpeg", attempt, verdict, duration)
            log.info("video edit attempt %s rejected: %s", attempt, verdict.summary())

        raise MediaQualityError(
            f"Video belum lolos QA setelah {MAX_ATTEMPTS} percobaan",
            detail=last.summary() if last else "",
        )

    async def _apply_edit(
        self,
        instruction: str,
        source: Path,
        second: Path | None,
        out: Path,
        subtitle: str,
        music: Path | None,
    ) -> None:
        text = instruction.lower()
        has_audio = _has_audio(source)

        # ---- concat mode: two clips joined end to end
        if second and Path(second).exists() and any(
            word in text for word in ("gabung", "concat", "satukan", "join")
        ):
            await run_ffmpeg(
                [
                    "-i", str(source), "-i", str(second),
                    "-filter_complex",
                    "[0:v]scale=1280:720,setsar=1,fps=30[v0];"
                    "[1:v]scale=1280:720,setsar=1,fps=30[v1];"
                    "[v0][v1]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
                ],
                settings=self.settings,
            )
            return

        # ---- single clip mode
        pre_input: list[str] = []
        trim = _extract_trim(text)
        if trim:
            start, end = trim
            pre_input += ["-ss", str(start), "-to", str(end)]

        vf: list[str] = []
        af: list[str] = []

        speed = _extract_speed(text)
        if speed:
            vf.append(f"setpts={1 / speed:.4f}*PTS")
            if has_audio:
                af.extend(_atempo_chain(speed))

        scale = _extract_scale(text)
        if any(word in text for word in ("vertikal", "9:16", "tiktok", "reels", "shorts")):
            vf += ["crop=ih*9/16:ih", "scale=720:1280"]
        elif any(word in text for word in ("kotak", "1:1", "square")):
            vf += ["crop=min(iw\\,ih):min(iw\\,ih)", "scale=1080:1080"]
        elif scale:
            vf.append(f"scale={scale}")
        elif second and Path(second).exists():
            vf.append("scale=1280:720")

        if any(word in text for word in ("hitam putih", "grayscale", " bw")):
            vf.append("hue=s=0")
        if any(word in text for word in ("terang", "bright")):
            vf.append("eq=brightness=0.08")
        if any(word in text for word in ("kontras", "contrast")):
            vf.append("eq=contrast=1.15")
        if any(word in text for word in ("mirror", "flip horizontal")):
            vf.append("hflip")

        if subtitle:
            subtitle_file = out.with_suffix(".srt")
            subtitle_file.write_text(subtitle, encoding="utf-8")
            escaped = str(subtitle_file).replace("\\", "/").replace(":", "\\:")
            vf.append(
                f"subtitles='{escaped}':force_style='FontName=DejaVu Sans,FontSize=22,"
                "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Outline=1'"
            )

        command = [*pre_input, "-i", str(source)]

        # ---- music bed
        if music and Path(music).exists():
            command += ["-i", str(music)]
            video_chain = ",".join([*vf, "format=yuv420p"])
            if has_audio:
                af_chain = ",".join([*af, "volume=1.0"]) if af else "anull"
                filter_complex = (
                    f"[0:v]{video_chain}[v];"
                    f"[1:a]volume=0.25[bg];"
                    f"[0:a]{af_chain}[voice];"
                    "[voice][bg]amix=inputs=2:duration=first:dropout_transition=2[a]"
                )
            else:
                filter_complex = f"[0:v]{video_chain}[v];[1:a]volume=0.25[a]"
            try:
                await run_ffmpeg(
                    [
                        *command,
                        "-filter_complex", filter_complex,
                        "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                        "-c:a", "aac", "-b:a", "160k", "-shortest",
                        "-movflags", "+faststart", str(out),
                    ],
                    settings=self.settings,
                )
                return
            except ToolError as exc:
                log.warning("music mix failed, retrying video-only: %s", exc)
                command = [*pre_input, "-i", str(source)]

        command += ["-vf", ",".join([*vf, "format=yuv420p"])] if vf else ["-vf", "format=yuv420p"]
        if af and has_audio:
            command += ["-af", ",".join(af)]
        command += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        ]
        command += ["-c:a", "aac", "-b:a", "160k"] if has_audio else ["-an"]
        command += [str(out)]
        try:
            await run_ffmpeg(command, settings=self.settings)
        except ToolError as exc:
            log.warning("video edit failed (%s); retrying with a minimal re-encode", exc)
            await run_ffmpeg(
                [*pre_input, "-i", str(source), "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "26", "-pix_fmt", "yuv420p", str(out)],
                settings=self.settings,
            )

    async def _duration(self, path: Path) -> float:
        probe = await probe_media(path, settings=self.settings)
        return media_duration(probe)

    # -------------------------------------------------------------- AI clip
    async def generate_ai_clip(self, prompt: str, *, seconds: int = 4) -> VideoResult:
        if not self.settings.replicate_api_token:
            raise MissingSecretError("REPLICATE_API_TOKEN")
        headers = {
            "Authorization": f"Bearer {self.settings.replicate_api_token}",
            "Content-Type": "application/json",
            "Prefer": "wait=120",
        }
        payload = {
            "input": {"prompt": prompt, "num_frames": min(80, max(16, seconds * 16))}
        }
        url = "https://api.replicate.com/v1/models/lightricks/ltx-video/predictions"
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            output = data.get("output")
            if isinstance(output, list) and output:
                output = output[0]
            if not output:
                raise ToolError("Replicate tidak mengembalikan video")
            path = self._out("ai", prompt)
            media = await client.get(str(output))
            media.raise_for_status()
            path.write_bytes(media.content)

        verdict = await check_video_async(path)
        if not verdict.ok:
            raise MediaQualityError("Clip AI gagal QA", detail=verdict.summary())
        return VideoResult(path, "ai", "replicate", 1, verdict, await self._duration(path))


# ------------------------------------------------------------------ helpers
def _manim_available() -> bool:
    return bool(shutil.which("manim"))


def _is_mathy(request: str, spec: dict) -> bool:
    text = (request + " " + json.dumps(spec, ensure_ascii=False)).lower()
    if any(word in text for word in ("rumus", "persamaan", "equation", "pythagoras", "matematika")):
        return True
    return any(scene.get("type") == "equation" for scene in spec.get("scenes", []))


def _validate_spec(data: dict, request: str) -> dict | None:
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return None
    clean: list[dict] = []
    for scene in scenes[:6]:
        if not isinstance(scene, dict):
            continue
        scene_type = str(scene.get("type", "bullets"))
        if scene_type not in {"title", "bullets", "equation", "steps", "chart", "outro"}:
            scene_type = "bullets"
        lines = scene.get("lines") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(line)[:140] for line in lines if str(line).strip()][:6]
        if not lines:
            continue
        try:
            duration = float(scene.get("duration") or 5.0)
        except (TypeError, ValueError):
            duration = 5.0
        clean.append(
            {
                "type": scene_type,
                "heading": str(scene.get("heading") or data.get("title") or request)[:80],
                "lines": lines,
                "duration": max(2.0, min(12.0, duration)),
            }
        )
    if not clean:
        return None
    narration = str(data.get("narration") or "").strip()[:900]
    return {
        "title": str(data.get("title") or request)[:80],
        "scenes": clean,
        "narration": narration,
    }


def _template_spec(request: str) -> dict:
    """Deterministic fallback so video creation never hard-fails on LLM outage."""
    title = request.strip()[:70]
    return {
        "title": title,
        "scenes": [
            {"type": "title", "heading": title, "lines": ["Dibuat oleh AI agent Telegram"], "duration": 4.0},
            {
                "type": "bullets",
                "heading": "Ringkasan",
                "lines": [
                    "Permintaan kamu sudah diproses",
                    "Struktur dibuat otomatis",
                    "Silakan minta revisi kalau perlu",
                ],
                "duration": 5.0,
            },
            {"type": "outro", "heading": "Selesai", "lines": ["Terima kasih!"], "duration": 3.0},
        ],
        "narration": f"Ini video tentang {title}. Video dibuat otomatis oleh agent Telegram.",
    }


def _manim_scene_code(spec: dict) -> str:
    """Build Manim code from a whitelisted spec - no LLM-authored code runs."""
    scenes_repr = json.dumps(spec.get("scenes", []), ensure_ascii=False)
    return f'''"""Auto-generated Manim scene (data-driven, no arbitrary code execution)."""
import json
from manim import *

SCENES = json.loads(r"""{scenes_repr}""")


class AgentScene(Scene):
    def construct(self):
        for index, scene in enumerate(SCENES):
            heading = Text(scene.get("heading", "")[:44], font_size=38)
            heading.to_edge(UP, buff=0.8)
            self.play(Write(heading), run_time=0.8)

            body_lines = scene.get("lines", [])[:5]
            if scene.get("type") == "equation" and body_lines:
                try:
                    body = MathTex(body_lines[0][:60], font_size=54)
                except Exception:
                    body = Text(body_lines[0][:60], font_size=40)
            else:
                body = VGroup(*[Text(line[:58], font_size=28) for line in body_lines])
                body.arrange(DOWN, aligned_edge=LEFT, buff=0.35)
            body.next_to(heading, DOWN, buff=0.8)

            self.play(FadeIn(body, shift=UP * 0.3), run_time=0.9)
            self.wait(max(1.0, float(scene.get("duration", 4)) - 1.8))
            self.play(FadeOut(heading), FadeOut(body), run_time=0.6)
'''


def _load_font(size: int):
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 <= width:
            current = f"{current} {word}".strip()
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _atempo_chain(speed: float) -> list[str]:
    """ffmpeg atempo only accepts 0.5-2.0 per instance; chain for bigger factors."""
    chain: list[str] = []
    remaining = speed
    while remaining > 2.0:
        chain.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        chain.append("atempo=0.5")
        remaining /= 0.5
    chain.append(f"atempo={remaining:.4f}")
    return chain

def _extract_speed(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:x|kali)", text)
    if match:
        try:
            return max(0.25, min(4.0, float(match.group(1).replace(",", "."))))
        except ValueError:
            return None
    if any(word in text for word in ("percepat", "cepatkan", "speed up")):
        return 1.5
    if any(word in text for word in ("perlambat", "slowmo", "slow motion")):
        return 0.5
    return None


def _extract_trim(text: str) -> tuple[float, float] | None:
    match = re.search(
        r"(?:dari|from|mulai)\s*(\d+(?:[.,]\d+)?)\s*(?:s|detik|sec)?\s*(?:sampai|s/d|hingga|to|-)\s*(\d+(?:[.,]\d+)?)",
        text,
    )
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


def _extract_scale(text: str) -> str | None:
    match = re.search(r"(?:jadi|resize|scale|ke)\s*(\d{3,4})\s*[x×]\s*(\d{3,4})", text)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    if any(word in text for word in ("1080p", "full hd")):
        return "1920:1080"
    if any(word in text for word in ("720p",)):
        return "1280:720"
    return None


_AUDIO_CACHE: dict[str, bool] = {}


def _has_audio(path: Path) -> bool:
    """Whether *path* carries an audio stream. Cached: ffprobe is not free."""
    key = str(path)
    if key in _AUDIO_CACHE:
        return _AUDIO_CACHE[key]
    import subprocess

    try:
        from app.services.ffmpeg import resolve_ffmpeg

        resolved = resolve_ffmpeg()
        binary = resolved.ffprobe or resolved.ffmpeg
        result = subprocess.run(
            [binary, "-v", "quiet", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        value = bool(result.stdout.strip())
    except Exception:  # noqa: BLE001
        value = False
    _AUDIO_CACHE[key] = value
    return value


def _in_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run_ffmpeg_blocking(args: list[str], settings) -> None:
    from app.services.ffmpeg import resolve_ffmpeg

    paths = resolve_ffmpeg(settings)
    import subprocess

    command = [paths.ffmpeg, "-hide_banner", "-nostdin", "-y", *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise ToolError(f"ffmpeg gagal: {(result.stderr or '')[-400:]}")
