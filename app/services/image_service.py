"""Image generation and editing.

Generation providers, in order:
1. Hugging Face Inference (FLUX.1-schnell) when ``HF_TOKEN`` is set.
2. Pollinations.ai - free, keyless, decent quality, no rate limit headaches.

Editing providers:
1. Hugging Face ``instruct-pix2pix`` image-to-image when a token is present.
2. Replicate when ``REPLICATE_API_TOKEN`` is set.
3. Deterministic local FFmpeg/Pillow pipeline (brightness, contrast, saturation,
   crop, resize, text overlay). This always works offline, so "edit this image"
   is never a dead end - it just becomes a narrower edit.

Every path runs the QA gate and retries up to ``MAX_ATTEMPTS`` with a sharpened
prompt when the result is blank or blurry.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.errors import MediaQualityError, MissingSecretError, ToolError
from app.core.logging_setup import get_logger
from app.services.media_qa import QualityVerdict, check_image_async

log = get_logger(__name__)

MAX_ATTEMPTS = 3
HF_FLUX_URL = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"
HF_PIX2PIX_URL = (
    "https://api-inference.huggingface.co/models/timbrooks/instruct-pix2pix"
)

NEGATIVE_HINTS = "blurry, low quality, watermark, text artifacts, deformed"
QUALITY_SUFFIX = "highly detailed, sharp focus, professional, 8k"


@dataclass
class ImageResult:
    path: Path
    prompt: str
    provider: str
    attempts: int
    verdict: QualityVerdict
    width: int = 0
    height: int = 0


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit] or "image").strip("-")


def _target_path(outputs: Path, prompt: str, tag: str) -> Path:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return outputs / "images" / f"{stamp}-{tag}-{_slug(prompt)}.png"


class ImageService:
    def __init__(self, settings, *, outputs_dir: Path | None = None):
        self.settings = settings
        self.outputs = Path(outputs_dir or settings.outputs_path)
        (self.outputs / "images").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ generate
    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        negative: str = "",
        style: str = "",
        progress=None,
    ) -> ImageResult:
        last_verdict: QualityVerdict | None = None
        provider_used = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            full_prompt = self._compose_prompt(prompt, style, attempt)
            path = _target_path(self.outputs, prompt, f"gen{attempt}")
            if progress:
                await progress(
                    int(10 + 25 * (attempt - 1) / MAX_ATTEMPTS),
                    f"Generate gambar (percobaan {attempt}/{MAX_ATTEMPTS})",
                )

            errors: list[str] = []
            for provider_name, fn in self._generation_providers():
                try:
                    await fn(full_prompt, path, size, negative)
                    provider_used = provider_name
                    break
                except MissingSecretError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{provider_name}: {exc}")
                    log.warning("image provider %s failed: %s", provider_name, exc)
            else:
                raise ToolError("Semua provider gambar gagal: " + "; ".join(errors)[:500])

            if progress:
                await progress(60, "Cek kualitas gambar")
            verdict = await check_image_async(path)
            last_verdict = verdict
            log.info("image attempt=%s provider=%s verdict=%s", attempt, provider_used, verdict.summary())
            if verdict.ok:
                width, height = verdict.metrics.get("width", 0), verdict.metrics.get("height", 0)
                return ImageResult(path, prompt, provider_used, attempt, verdict, width, height)
            if progress:
                await progress(65, f"Hasil kurang bagus, memperbaiki: {verdict.reasons[0]}")

        raise MediaQualityError(
            f"Gambar belum lolos QA setelah {MAX_ATTEMPTS} percobaan",
            detail=last_verdict.summary() if last_verdict else "",
        )

    def _compose_prompt(self, prompt: str, style: str, attempt: int) -> str:
        parts = [prompt.strip()]
        if style:
            parts.append(f"style: {style}")
        parts.append(QUALITY_SUFFIX)
        if attempt > 1:
            # Escalate specificity instead of just repeating the same request.
            parts.append("extra sharp detail, well lit, clear subject, no blur")
        return ", ".join(p for p in parts if p)

    def _generation_providers(self):
        providers = []
        if self.settings.hf_token:
            providers.append(("huggingface-flux", self._generate_hf))
        providers.append(("pollinations", self._generate_pollinations))
        # Last resort: a designed local render, so the user always gets an image
        # even when every network provider is down or rate limited.
        providers.append(("local-render", self._generate_local))
        return providers

    async def _generate_local(self, prompt: str, path: Path, size: str, negative: str) -> None:
        """Deterministic offline image built from the prompt.

        Not a diffusion model - a composed gradient poster with the subject text.
        It exists so "buatkan gambar" degrades to something usable instead of an
        error when the network providers are unavailable.
        """
        import asyncio as _asyncio

        from PIL import Image, ImageDraw, ImageFilter

        width, height = _parse_size(size)

        def _render() -> None:
            seed = _stable_seed(prompt)
            top = ((seed * 37) % 200 + 20, (seed * 53) % 160 + 40, (seed * 71) % 200 + 40)
            bottom = ((seed * 91) % 200 + 20, (seed * 17) % 160 + 30, (seed * 29) % 200 + 50)
            image = Image.new("RGB", (width, height))
            draw = ImageDraw.Draw(image)
            for y in range(height):
                ratio = y / max(1, height - 1)
                draw.line(
                    [(0, y), (width, y)],
                    fill=tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3)),
                )

            # soft blobs for visual interest
            blobs = Image.new("RGB", (width, height), (0, 0, 0))
            blob_draw = ImageDraw.Draw(blobs)
            for index in range(5):
                cx = (seed * (index + 3) * 13) % width
                cy = (seed * (index + 7) * 17) % height
                radius = (width // 6) + (index * 11) % (width // 8)
                tone = (
                    (seed * (index + 2) * 31) % 200 + 30,
                    (seed * (index + 5) * 41) % 200 + 30,
                    (seed * (index + 9) * 23) % 200 + 30,
                )
                blob_draw.ellipse(
                    [cx - radius, cy - radius, cx + radius, cy + radius], fill=tone
                )
            blobs = blobs.filter(ImageFilter.GaussianBlur(radius=width // 12))
            image = Image.blend(image, blobs, 0.55)

            overlay = ImageDraw.Draw(image)
            font = _load_font(max(20, width // 22))
            title = prompt.strip()[:60]
            wrapped = _wrap_text(title, 26)
            text_y = height // 2 - (len(wrapped) * (width // 18)) // 2
            for line in wrapped[:3]:
                bbox = overlay.textbbox((0, 0), line, font=font)
                overlay.text(
                    ((width - (bbox[2] - bbox[0])) // 2, text_y),
                    line,
                    font=font,
                    fill=(248, 250, 252),
                    stroke_width=2,
                    stroke_fill=(15, 23, 42),
                )
                text_y += width // 16
            image.save(path)

        await _asyncio.to_thread(_render)

    async def _generate_hf(self, prompt: str, path: Path, size: str, negative: str) -> None:
        if not self.settings.hf_token:
            raise MissingSecretError("HF_TOKEN")
        width, height = _parse_size(size)
        payload = {
            "inputs": prompt,
            "parameters": {
                "width": width,
                "height": height,
                "num_inference_steps": 4,
                "negative_prompt": negative or NEGATIVE_HINTS,
            },
        }
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(HF_FLUX_URL, json=payload, headers=headers)
            if response.status_code == 503:
                await asyncio.sleep(6)  # model cold start
                response = await client.post(HF_FLUX_URL, json=payload, headers=headers)
            response.raise_for_status()
            if "application/json" in response.headers.get("content-type", ""):
                raise ToolError(f"HF mengembalikan error: {response.text[:200]}")
            path.write_bytes(response.content)

    async def _generate_pollinations(
        self, prompt: str, path: Path, size: str, negative: str
    ) -> None:
        width, height = _parse_size(size)
        query = urllib.parse.urlencode(
            {
                "width": width,
                "height": height,
                "nologo": "true",
                "enhance": "true",
                "seed": _stable_seed(prompt),
            }
        )
        url = f"{self.settings.pollinations_base_url}/{urllib.parse.quote(prompt)}?{query}"

        # Pollinations returns 5xx under load; a couple of spaced retries turn a
        # transient 503 into a success instead of falling through to the local
        # render.
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.get(url)
                    if response.status_code in (429, 500, 502, 503, 504):
                        raise ToolError(f"Pollinations sibuk ({response.status_code})")
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "image" not in content_type:
                        raise ToolError(f"Pollinations tidak mengembalikan gambar ({content_type})")
                    path.write_bytes(response.content)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(2.5 * (attempt + 1))
            raise ToolError(f"Pollinations gagal setelah 3 percobaan: {last_error}")

    # ---------------------------------------------------------------- edit
    async def edit(
        self,
        instruction: str,
        *,
        source: Path | None = None,
        progress=None,
    ) -> ImageResult:
        if source is None or not Path(source).exists():
            raise ToolError("Sumber gambar untuk diedit tidak ditemukan.")
        source = Path(source)
        last_verdict: QualityVerdict | None = None
        provider_used = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            path = _target_path(self.outputs, instruction, f"edit{attempt}")
            if progress:
                await progress(int(10 + 25 * (attempt - 1) / MAX_ATTEMPTS), f"Edit gambar ({attempt}/{MAX_ATTEMPTS})")

            errors: list[str] = []
            for provider_name, fn in self._edit_providers():
                try:
                    await fn(source, instruction, path)
                    provider_used = provider_name
                    break
                except MissingSecretError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{provider_name}: {exc}")
                    log.warning("edit provider %s failed: %s", provider_name, exc)
            else:
                raise ToolError("Semua provider edit gambar gagal: " + "; ".join(errors)[:400])

            if progress:
                await progress(60, "Cek kualitas hasil edit")
            verdict = await check_image_async(path)
            last_verdict = verdict
            if verdict.ok:
                return ImageResult(
                    path,
                    instruction,
                    provider_used,
                    attempt,
                    verdict,
                    verdict.metrics.get("width", 0),
                    verdict.metrics.get("height", 0),
                )

        raise MediaQualityError(
            f"Hasil edit belum lolos QA setelah {MAX_ATTEMPTS} percobaan",
            detail=last_verdict.summary() if last_verdict else "",
        )

    def _edit_providers(self):
        providers = []
        if self.settings.hf_token:
            providers.append(("huggingface-pix2pix", self._edit_hf_pix2pix))
        if self.settings.replicate_api_token:
            providers.append(("replicate", self._edit_replicate))
        providers.append(("local-ffmpeg", self._edit_local))
        return providers

    async def _edit_hf_pix2pix(self, source: Path, instruction: str, path: Path) -> None:
        if not self.settings.hf_token:
            raise MissingSecretError("HF_TOKEN")
        import base64

        image_b64 = base64.b64encode(source.read_bytes()).decode()
        payload = {"inputs": instruction, "image": image_b64}
        headers = {"Authorization": f"Bearer {self.settings.hf_token}"}
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(HF_PIX2PIX_URL, json=payload, headers=headers)
            response.raise_for_status()
            if "image" not in response.headers.get("content-type", ""):
                raise ToolError(f"HF pix2pix: {response.text[:200]}")
            path.write_bytes(response.content)

    async def _edit_replicate(self, source: Path, instruction: str, path: Path) -> None:
        if not self.settings.replicate_api_token:
            raise MissingSecretError("REPLICATE_API_TOKEN")
        import base64

        data_uri = "data:image/png;base64," + base64.b64encode(source.read_bytes()).decode()
        headers = {
            "Authorization": f"Bearer {self.settings.replicate_api_token}",
            "Content-Type": "application/json",
            "Prefer": "wait=60",
        }
        payload = {"input": {"image": data_uri, "prompt": instruction}}
        url = "https://api.replicate.com/v1/models/timbrooks/instruct-pix2pix/predictions"
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            output = data.get("output")
            if isinstance(output, list) and output:
                output = output[0]
            if not output:
                raise ToolError("Replicate tidak mengembalikan output")
            image = await client.get(str(output))
            image.raise_for_status()
            path.write_bytes(image.content)

    async def _edit_local(self, source: Path, instruction: str, path: Path) -> None:
        """Deterministic Pillow pipeline: the instruction is mapped to operations."""
        await asyncio.to_thread(self._edit_local_sync, source, instruction, path)

    def _edit_local_sync(self, source: Path, instruction: str, path: Path) -> None:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        text = instruction.lower()
        with Image.open(source) as img:
            img = img.convert("RGB")
            if any(word in text for word in ("terang", "bright", "cerah", "light")):
                img = ImageEnhance.Brightness(img).enhance(1.25)
            if any(word in text for word in ("gelap", "dark", "redup")):
                img = ImageEnhance.Brightness(img).enhance(0.78)
            if any(word in text for word in ("kontras", "contrast", "tegas")):
                img = ImageEnhance.Contrast(img).enhance(1.25)
            if any(word in text for word in ("vivid", "satur", "warna", "color")):
                img = ImageEnhance.Color(img).enhance(1.3)
            if any(word in text for word in ("abu", "grayscale", "hitam putih", "bw")):
                img = ImageOps.grayscale(img).convert("RGB")
            if any(word in text for word in ("tajam", "sharp", "jelas", "blur")):
                img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
            if any(word in text for word in ("halus", "smooth", "soft")):
                img = img.filter(ImageFilter.GaussianBlur(0.8))
            if any(word in text for word in ("mirror", "flip")):
                img = ImageOps.mirror(img)
            if any(word in text for word in ("putar", "rotate", "miring")):
                img = img.rotate(-3, expand=True, fillcolor=(255, 255, 255))
            if any(word in text for word in ("watermark", "tanda air")):
                img = self._add_watermark(img)
            if any(word in text for word in ("kecil", "kecilin", "compress", "resize")):
                max_side = 1280
                if max(img.size) > max_side:
                    ratio = max_side / max(img.size)
                    img = img.resize((int(img.width * ratio), int(img.height * ratio)))
            if any(word in text for word in ("besar", "besar in", "perbesar", "upscale", "hd")):
                img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)

            # Never return the untouched original: apply a mild corrective pass.
            if img.tobytes() == Image.open(source).convert("RGB").tobytes():
                img = ImageEnhance.Contrast(ImageEnhance.Sharpness(img).enhance(1.15)).enhance(1.08)
            img.save(path, "PNG")

    @staticmethod
    def _add_watermark(img):
        from PIL import Image, ImageDraw

        canvas = img.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((16, canvas.height - 32), "AI generated", fill=(255, 255, 255, 160))
        return canvas


def _parse_size(size: str) -> tuple[int, int]:
    match = re.match(r"(\d{2,4})\s*[x×]\s*(\d{2,4})", size or "")
    if not match:
        return 1024, 1024
    width, height = int(match.group(1)), int(match.group(2))
    width = max(256, min(2048, width))
    height = max(256, min(2048, height))
    # most diffusion backends want multiples of 8
    return width - width % 8, height - height % 8


def _stable_seed(text: str) -> int:
    import hashlib

    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000


def _load_font(size: int):
    """Best available TrueType font, falling back to Pillow's bitmap default."""
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text[:width]]
