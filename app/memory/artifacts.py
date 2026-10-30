"""Artifact registry: the "what did we just make" index.

Every deliverable the bot produces (image, video, audio, web app, n8n flow,
ingested link, research note) is registered here with a kind, a title, a
location and metadata. This is what makes "perbaiki yang tadi" resolvable to a
real file instead of a guess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db.store import Store

# Canonical artifact kinds. Keep this closed: the resolver, the slash commands
# and the deploy layer all key off these strings.
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_WEB = "web"
KIND_WEB_APP = "web_app"
KIND_N8N = "n8n"
KIND_LINK = "link"
KIND_RESEARCH = "research"
KIND_CODE = "code"
KIND_TEXT = "text"

# kind -> slash-command friendly alias used by the resolver
KIND_ALIASES: dict[str, str] = {
    "gambar": KIND_IMAGE,
    "image": KIND_IMAGE,
    "foto": KIND_IMAGE,
    "pic": KIND_IMAGE,
    "video": KIND_VIDEO,
    "film": KIND_VIDEO,
    "suara": KIND_AUDIO,
    "audio": KIND_AUDIO,
    "voice": KIND_AUDIO,
    "lagu": KIND_AUDIO,
    "musik": KIND_AUDIO,
    "tts": KIND_AUDIO,
    "web": KIND_WEB,
    "website": KIND_WEB,
    "situs": KIND_WEB,
    "landing": KIND_WEB,
    "webapp": KIND_WEB_APP,
    "fullstack": KIND_WEB_APP,
    "n8n": KIND_N8N,
    "workflow": KIND_N8N,
    "link": KIND_LINK,
    "research": KIND_RESEARCH,
    "riset": KIND_RESEARCH,
}


@dataclass
class Artifact:
    id: int
    user_id: int
    kind: str
    title: str
    location: str
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def path(self) -> Path | None:
        if self.location and not self.location.startswith(("http://", "https://")):
            return Path(self.location)
        return None

    def exists(self) -> bool:
        path = self.path
        if path is None:
            return bool(self.location)
        return path.exists()

    def brief(self) -> str:
        title = self.title or "(tanpa judul)"
        if self.location.startswith("http"):
            return f"{self.kind}: {title} -> {self.location}"
        return f"{self.kind}: {title} -> {Path(self.location).name}"


def _to_artifact(row: dict) -> Artifact:
    return Artifact(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kind=row["kind"],
        title=row.get("title") or "",
        location=row.get("location") or "",
        meta=json.loads(row.get("meta") or "{}"),
        created_at=row.get("created_at") or "",
    )


class ArtifactRegistry:
    def __init__(self, store: Store):
        self.store = store

    async def register(
        self,
        user_id: int,
        kind: str,
        *,
        title: str = "",
        location: str = "",
        meta: dict | None = None,
    ) -> Artifact:
        artifact_id = await self.store.add_artifact(user_id, kind, title, location, meta or {})
        row = await self.store.latest_artifact(user_id, kind)
        if row and int(row["id"]) == artifact_id:
            return _to_artifact(row)
        rows = await self.store.search_artifacts(user_id, title, limit=1)
        if rows:
            return _to_artifact(rows[0])
        return Artifact(artifact_id, user_id, kind, title, location, meta or {})

    async def latest(self, user_id: int, kind: str | None = None) -> Artifact | None:
        row = await self.store.latest_artifact(user_id, kind)
        return _to_artifact(row) if row else None

    async def list(self, user_id: int, limit: int = 10) -> list[Artifact]:
        rows = await self.store.list_artifacts(user_id, limit)
        return [_to_artifact(r) for r in rows]

    async def find(self, user_id: int, keyword: str, limit: int = 5) -> list[Artifact]:
        rows = await self.store.search_artifacts(user_id, keyword, limit)
        return [_to_artifact(r) for r in rows]

    async def summary_line(self, user_id: int) -> str:
        artifacts = await self.list(user_id, limit=8)
        if not artifacts:
            return "Belum ada artifact."
        return "\n".join(f"- {a.brief()}" for a in artifacts)
