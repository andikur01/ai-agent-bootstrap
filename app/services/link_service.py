"""Link ingestion: turn any video/article link into something the LLM can read.

Ladder, cheapest and most likely to work first:

1. yt-dlp metadata + subtitles (manual or auto). Most videos already have
   captions, so this is free and instant.
2. yt-dlp download + local Whisper transcription when there are no captions.
3. Article path: plain HTTP fetch + readability-ish text extraction.
4. Fallback when the platform blocks us: ask the user to send the file, or -
   with their explicit consent and a YouTube OAuth token - upload it to their
   own private YouTube channel and read the transcript back.

Nothing here ever creates an account or accepts a CAPTCHA: that is always the
user's manual step, and the error message says so.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import MissingSecretError, ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

MAX_TRANSCRIPT_CHARS = 24000
VIDEO_HOSTS = re.compile(
    r"(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|"
    r"twitter\.com|x\.com|vimeo\.com|dailymotion\.com|twitch\.tv|bilibili\.com)",
    re.I,
)


@dataclass
class LinkContent:
    url: str
    kind: str  # video | article
    title: str = ""
    author: str = ""
    duration: float = 0.0
    transcript: str = ""
    description: str = ""
    method: str = ""  # subtitles | whisper | article | user-file | youtube-private
    thumbnail: str = ""
    media_path: Path | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.transcript.strip() or self.description.strip())

    def context(self, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
        parts = [f"Judul: {self.title}", f"URL: {self.url}"]
        if self.author:
            parts.append(f"Kanal/penulis: {self.author}")
        if self.duration:
            parts.append(f"Durasi: {self.duration:.0f} detik")
        if self.description:
            parts.append("Deskripsi:\n" + self.description[:1200])
        if self.transcript:
            parts.append("Transkrip:\n" + self.transcript[:max_chars])
        return "\n\n".join(parts)


class LinkIngestService:
    def __init__(self, settings, *, audio=None, outputs_dir: Path | None = None):
        self.settings = settings
        self.audio = audio
        self.outputs = Path(outputs_dir or settings.outputs_path)
        (self.outputs / "links").mkdir(parents=True, exist_ok=True)

    async def ingest(self, url: str, *, allow_download: bool = True, progress=None) -> LinkContent:
        url = url.strip()
        if VIDEO_HOSTS.search(url):
            return await self._ingest_video(url, allow_download=allow_download, progress=progress)
        return await self._ingest_article(url, progress=progress)

    # ----------------------------------------------------------------- video
    async def _ingest_video(
        self, url: str, *, allow_download: bool, progress=None
    ) -> LinkContent:
        if progress:
            await progress(15, "Ambil metadata video")
        info = await self._probe(url)
        content = LinkContent(
            url=url,
            kind="video",
            title=str(info.get("title") or ""),
            author=str(info.get("uploader") or info.get("channel") or ""),
            duration=float(info.get("duration") or 0.0),
            description=str(info.get("description") or "")[:4000],
            thumbnail=str(info.get("thumbnail") or ""),
            metadata={
                "id": info.get("id"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
                "platform": info.get("extractor_key"),
            },
        )

        # 1. captions already attached to the video
        if progress:
            await progress(35, "Cari subtitle/transkrip")
        transcript = await self._captions(url, info)
        if transcript:
            content.transcript = transcript
            content.method = "subtitles"
            return content

        if not allow_download:
            content.method = "metadata-only"
            return content

        # 2. download the audio and transcribe locally
        if progress:
            await progress(50, "Tidak ada subtitle, unduh audio")
        try:
            media = await self._download_audio(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("download blocked for %s: %s", url, exc)
            content.method = "blocked"
            content.metadata["blocked_reason"] = str(exc)[:300]
            return content

        content.media_path = media
        if self.audio is not None:
            try:
                if progress:
                    await progress(65, "Transkripsi audio (Whisper)")
                content.transcript = await self.audio.speech_to_text(media)
                content.method = "whisper"
            except Exception as exc:  # noqa: BLE001
                log.warning("whisper transcription failed: %s", exc)
                content.metadata["transcribe_error"] = str(exc)[:300]
                content.method = "downloaded"
        else:
            content.method = "downloaded"
        return content

    async def _probe(self, url: str) -> dict:
        def _run() -> dict:
            from yt_dlp import YoutubeDL  # type: ignore

            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
            }
            cookies = self.settings.ytdlp_cookies_file
            if cookies and Path(cookies).exists():
                options["cookiefile"] = cookies
            with YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=False) or {}

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(
                f"Tidak bisa membaca link ini: {str(exc)[:200]}",
                detail="Platform mungkin memblokir. Coba kirim filenya langsung.",
            ) from exc

    async def _captions(self, url: str, info: dict) -> str:
        subtitles = {**(info.get("subtitles") or {}), **(info.get("automatic_captions") or {})}
        if not subtitles:
            return ""
        preferred = ["id", "en", "en-US", "en-orig"]
        keys = [key for key in preferred if key in subtitles] or list(subtitles)[:1]
        for key in keys:
            for track in subtitles.get(key, []):
                if track.get("ext") not in {"vtt", "json3", "srv1", "srv3", "ttml"}:
                    continue
                try:
                    text = await self._fetch_caption(track["url"], track.get("ext", "vtt"))
                    if text:
                        return text
                except Exception as exc:  # noqa: BLE001
                    log.debug("caption fetch failed: %s", exc)
        return ""

    async def _fetch_caption(self, url: str, ext: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.text

            # YouTube now serves captions as an HLS playlist of VTT/JSON chunks.
            if body.lstrip().startswith("#EXTM3U"):
                chunks: list[str] = []
                for line in body.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    chunk_url = httpx.URL(url).join(line).human_repr()
                    try:
                        chunk = await client.get(chunk_url)
                        chunk.raise_for_status()
                    except Exception as exc:  # noqa: BLE001
                        log.debug("caption chunk failed: %s", exc)
                        continue
                    chunks.append(chunk.text)
                body = "\n".join(chunks)

        if ext == "json3" or body.lstrip().startswith("{"):
            try:
                data = json.loads(body)
                pieces = []
                for event in data.get("events", []):
                    for seg in event.get("segs", []) or []:
                        pieces.append(seg.get("utf8", ""))
                return _clean_transcript("".join(pieces))
            except json.JSONDecodeError:
                pass
        return _clean_transcript(_vtt_to_text(body))

    async def _download_audio(self, url: str) -> Path:
        target_dir = self.outputs / "links"
        target_dir.mkdir(parents=True, exist_ok=True)

        def _run() -> Path:
            from yt_dlp import YoutubeDL  # type: ignore

            template = str(target_dir / "%(id)s.%(ext)s")
            options = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestaudio/best",
                "outtmpl": template,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "5"}
                ],
                "retries": 2,
                "socket_timeout": 30,
            }
            cookies = self.settings.ytdlp_cookies_file
            if cookies and Path(cookies).exists():
                options["cookiefile"] = cookies
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                options["ffmpeg_location"] = ffmpeg
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            media_id = info.get("id", "audio")
            candidate = target_dir / f"{media_id}.mp3"
            if candidate.exists():
                return candidate
            matches = list(target_dir.glob(f"{media_id}.*"))
            if matches:
                return matches[0]
            raise ToolError("yt-dlp tidak menghasilkan file audio")

        return await asyncio.to_thread(_run)

    # --------------------------------------------------------------- article
    async def _ingest_article(self, url: str, progress=None) -> LinkContent:
        if progress:
            await progress(25, "Baca halaman")
        import httpx

        from app.services.search_service import SearchService

        headers = {"User-Agent": "Mozilla/5.0 (compatible; PersonalAgentBot/1.0)"}
        async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        text = SearchService.extract_text(html)
        title = _extract_title(html) or url
        content = LinkContent(
            url=url,
            kind="article",
            title=title,
            transcript=text[:MAX_TRANSCRIPT_CHARS],
            method="article",
        )
        if not content.ok:
            raise ToolError("Halaman tidak bisa dibaca isinya (mungkin butuh login atau JS).")
        return content

    # ------------------------------------------------------- youtube fallback
    async def upload_private_and_transcribe(self, media: Path, title: str) -> LinkContent:
        """Upload to the user's own private YouTube channel, then read captions.

        This is the last rung of the ladder. It needs an OAuth token that the
        *user* generated by hand; we never run the consent flow for them.
        """
        token_file = Path(self.settings.youtube_token_file)
        if not token_file.exists():
            raise MissingSecretError(
                "YOUTUBE_TOKEN_FILE",
                detail="Jalankan scripts/youtube_auth.py sekali di mesin kamu untuk login manual.",
            )
        if not media.exists():
            raise ToolError(f"File tidak ada: {media}")

        from app.services.youtube_upload import YouTubeUploader

        uploader = YouTubeUploader(token_file)
        video_id = await uploader.upload_private(media, title)
        url = f"https://youtu.be/{video_id}"
        # YouTube needs a moment before captions become readable
        await asyncio.sleep(5)
        content = await self._ingest_video(url, allow_download=False)
        content.method = "youtube-private"
        content.metadata["youtube_video_id"] = video_id
        content.metadata["youtube_url"] = url
        return content


def _vtt_to_text(vtt: str) -> str:
    lines: list[str] = []
    for line in vtt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if "-->" in stripped or stripped.isdigit():
            continue
        stripped = re.sub(r"<[^>]+>", "", stripped)
        lines.append(stripped)
    # collapse the rolling-window duplication YouTube emits
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return " ".join(deduped)


def _clean_transcript(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TRANSCRIPT_CHARS]


def _extract_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not match:
        return ""
    import html as html_module

    return html_module.unescape(match.group(1)).strip()[:200]


ANSWER_SYSTEM = """Kamu menjawab pertanyaan user tentang isi sebuah link (video/artikel).

Aturan:
- Jawab HANYA berdasarkan isi yang diberikan. Kalau informasinya tidak ada,
  bilang jujur tidak ada di konten itu.
- Bahasa Indonesia santai tapi jelas.
- Ringkas: maksimal 250 kata, pakai poin kalau membantu.
- Kalau user tidak bertanya apa-apa, buat ringkasan isi + 3 poin penting."""


async def answer_about_link(
    content: LinkContent, question: str, gateway, *, user_id: int = 0
) -> str:
    question = question.strip() or "Ringkas isi konten ini dan sebutkan poin pentingnya."
    if gateway is not None and gateway.available() and content.ok:
        try:
            result = await gateway.complete(
                [
                    {"role": "system", "content": ANSWER_SYSTEM},
                    {
                        "role": "user",
                        "content": f"Isi konten:\n{content.context()}\n\nPertanyaan user: {question}",
                    },
                ],
                tier="big",
                user_id=user_id,
                temperature=0.3,
                max_tokens=1000,
            )
            if result.text.strip():
                return result.text.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("link answering failed: %s", exc)

    if content.transcript:
        head = content.transcript[:900]
        return f"Ringkasan otomatis (tanpa LLM):\n\n{head}..."
    return "Aku tidak bisa membaca isi link ini. Coba kirim file videonya langsung ya."
