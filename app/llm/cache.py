"""Semantic answer cache (the GPTCache role).

Similar question -> reuse the previous answer instead of paying for tokens
again. Deliberately *not* a fuzzy cache: a hit requires cosine similarity above
a high threshold on a normalised prompt, so we never serve a wrong answer for a
slightly different question.

Storage is SQLite next to the rest of the state. Entries are scoped per user so
one person's cached answer never leaks to another.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from app.core.logging_setup import get_logger
from app.core.utils import run_blocking
from app.memory.embeddings import Embedder, cosine

log = get_logger(__name__)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    namespace   TEXT NOT NULL DEFAULT 'chat',
    prompt_hash TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    response    TEXT NOT NULL,
    embedding   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_scope ON semantic_cache(user_id, namespace, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_hash ON semantic_cache(user_id, namespace, prompt_hash);
"""

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_prompt(text: str) -> str:
    text = text.strip().lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def prompt_hash(text: str) -> str:
    return hashlib.sha256(normalize_prompt(text).encode("utf-8")).hexdigest()


@dataclass
class CacheHit:
    response: str
    score: float
    exact: bool


class SemanticCache:
    def __init__(
        self,
        store,
        embedder: Embedder,
        *,
        threshold: float = 0.93,
        max_scan: int = 400,
        enabled: bool = True,
    ):
        self.store = store
        self.embedder = embedder
        self.threshold = threshold
        self.max_scan = max_scan
        self.enabled = enabled
        self._ready = False

    async def init(self) -> None:
        if self._ready:
            return
        await run_blocking(self._create_schema)
        self._ready = True

    def _create_schema(self) -> None:
        conn = self.store._conn  # noqa: SLF001 - deliberate: same connection, same lock
        with self.store._lock:  # noqa: SLF001
            conn.executescript(CACHE_SCHEMA)
            conn.commit()

    async def get(self, user_id: int, prompt: str, namespace: str = "chat") -> CacheHit | None:
        if not self.enabled:
            return None
        await self.init()
        digest = prompt_hash(prompt)

        exact = await run_blocking(
            self.store._query_one,  # noqa: SLF001
            """
            SELECT response FROM semantic_cache
            WHERE user_id = ? AND namespace = ? AND prompt_hash = ?
            """,
            (user_id, namespace, digest),
        )
        if exact:
            await self._bump(user_id, namespace, digest)
            return CacheHit(response=exact["response"], score=1.0, exact=True)

        rows = await run_blocking(
            self.store._query,  # noqa: SLF001
            """
            SELECT prompt, response, embedding FROM semantic_cache
            WHERE user_id = ? AND namespace = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, namespace, self.max_scan),
        )
        if not rows:
            return None

        vector = (await self.embedder.embed([normalize_prompt(prompt)]))[0]
        best: tuple[float, str] | None = None
        for row in rows:
            try:
                candidate = json.loads(row["embedding"])
            except json.JSONDecodeError:
                continue
            score = cosine(vector, candidate)
            if best is None or score > best[0]:
                best = (score, row["response"])
        if best and best[0] >= self.threshold:
            return CacheHit(response=best[1], score=best[0], exact=False)
        return None

    async def put(
        self, user_id: int, prompt: str, response: str, namespace: str = "chat"
    ) -> None:
        if not self.enabled or not response.strip():
            return
        await self.init()
        digest = prompt_hash(prompt)
        vector = (await self.embedder.embed([normalize_prompt(prompt)]))[0]
        await run_blocking(
            self.store._exec,  # noqa: SLF001
            """
            INSERT INTO semantic_cache
                (user_id, namespace, prompt_hash, prompt, response, embedding, created_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id, namespace, prompt_hash) DO UPDATE SET
                response = excluded.response,
                embedding = excluded.embedding,
                created_at = excluded.created_at
            """,
            (
                user_id,
                namespace,
                digest,
                prompt[:4000],
                response[:16000],
                json.dumps(vector),
                _now_iso(),
            ),
        )

    async def _bump(self, user_id: int, namespace: str, digest: str) -> None:
        await run_blocking(
            self.store._exec,  # noqa: SLF001
            """
            UPDATE semantic_cache SET hits = hits + 1
            WHERE user_id = ? AND namespace = ? AND prompt_hash = ?
            """,
            (user_id, namespace, digest),
        )

    async def stats(self, user_id: int) -> dict:
        await self.init()
        rows = await run_blocking(
            self.store._query,  # noqa: SLF001
            """
            SELECT COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits
            FROM semantic_cache WHERE user_id = ?
            """,
            (user_id,),
        )
        return rows[0] if rows else {"entries": 0, "hits": 0}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
