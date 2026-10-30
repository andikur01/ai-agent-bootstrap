"""YouTube upload helper for the link-ingestion fallback ladder.

Only used when a platform blocks us AND the user has explicitly asked for the
YouTube route. It never runs an interactive OAuth consent flow: the user
generates ``data/youtube_token.json`` themselves with
``scripts/youtube_auth.py``. Uploads are always ``private`` so nothing appears
on the user's public channel.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.core.errors import MissingSecretError, ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubeUploader:
    def __init__(self, token_file: str | Path):
        self.token_file = Path(token_file)
        if not self.token_file.exists():
            raise MissingSecretError(
                "YOUTUBE_TOKEN_FILE",
                detail="Jalankan `python scripts/youtube_auth.py` untuk login manual sekali.",
            )
        self.token = json.loads(self.token_file.read_text())

    def _access_token(self) -> str:
        token = self.token.get("token") or self.token.get("access_token")
        if not token:
            raise MissingSecretError("YOUTUBE_TOKEN_FILE", detail="Token file tidak berisi token.")
        return str(token)

    async def upload_private(self, media: Path, title: str, description: str = "") -> str:
        """Upload *media* as a private video and return its video id."""
        import httpx

        media = Path(media)
        if not media.exists():
            raise ToolError(f"File tidak ada: {media}")

        metadata = {
            "snippet": {
                "title": (title or media.stem)[:95],
                "description": (description or "Uploaded by personal agent bot")[:4800],
                "categoryId": "22",
            },
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
        }
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        params = {"uploadType": "multipart", "part": "snippet,status"}

        # Multipart body: metadata part + media part.
        boundary = "agent-bot-boundary-7f3a"
        body = bytearray()
        body += f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        body += json.dumps(metadata).encode()
        body += f"\r\n--{boundary}\r\nContent-Type: video/mp4\r\n\r\n".encode()
        body += media.read_bytes()
        body += f"\r\n--{boundary}--\r\n".encode()

        headers["Content-Type"] = f"multipart/related; boundary={boundary}"
        async with httpx.AsyncClient(timeout=1800) as client:
            response = await client.post(UPLOAD_URL, params=params, headers=headers, content=bytes(body))
            if response.status_code >= 400:
                raise ToolError(f"YouTube upload gagal ({response.status_code}): {response.text[:300]}")
            data = response.json()
        video_id = data.get("id")
        if not video_id:
            raise ToolError("YouTube tidak mengembalikan video id")
        log.info("uploaded private youtube video id=%s", video_id)
        return str(video_id)

    async def delete(self, video_id: str) -> bool:
        """Best-effort cleanup after we are done reading the transcript."""
        import httpx

        headers = {"Authorization": f"Bearer {self._access_token()}"}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.delete(
                f"https://www.googleapis.com/youtube/v3/videos",
                params={"id": video_id},
                headers=headers,
            )
        return response.status_code < 400
