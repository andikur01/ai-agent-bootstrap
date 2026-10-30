"""Vector store with a ChromaDB primary and a SQLite+numpy fallback.

Chroma gives us proper ANN search when it is available (locally embedded or via
``CHROMA_HOST``). When Chroma is not installed or not reachable we still need
working recall, so the fallback keeps vectors in SQLite and does exact cosine
search. For a personal bot's memory size (thousands of rows at most) exact
search is fine and has zero operational cost.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.logging_setup import get_logger
from app.core.utils import run_blocking
from app.memory.embeddings import Embedder, cosine

log = get_logger(__name__)

VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    embedding   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_user_kind ON vectors(user_id, kind, id DESC);
"""


@dataclass
class MemoryItem:
    id: str
    text: str
    score: float
    metadata: dict[str, Any]


class SQLiteVectorStore:
    backend = "sqlite"

    def __init__(self, store, embedder: Embedder):
        self.store = store
        self.embedder = embedder
        self._ready = False

    async def init(self) -> None:
        if self._ready:
            return
        await run_blocking(self._schema)
        self._ready = True

    def _schema(self) -> None:
        with self.store._lock:  # noqa: SLF001
            self.store._conn.executescript(VECTOR_SCHEMA)  # noqa: SLF001
            self.store._conn.commit()  # noqa: SLF001

    async def add(
        self, user_id: int, kind: str, text: str, metadata: dict | None = None
    ) -> str:
        await self.init()
        vector = (await self.embedder.embed([text]))[0]
        with self.store._lock:  # noqa: SLF001
            cur = self.store._conn.execute(  # noqa: SLF001
                """
                INSERT INTO vectors (user_id, kind, text, metadata, embedding, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    user_id,
                    kind,
                    text,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    json.dumps(vector),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self.store._conn.commit()  # noqa: SLF001
            return str(cur.lastrowid)

    async def search(
        self,
        user_id: int,
        query: str,
        *,
        kind: str | None = None,
        top_k: int = 6,
        min_score: float = 0.05,
    ) -> list[MemoryItem]:
        await self.init()
        if kind:
            rows = await run_blocking(
                self.store._query,  # noqa: SLF001
                """
                SELECT id, text, metadata, embedding FROM vectors
                WHERE user_id = ? AND kind = ? ORDER BY id DESC LIMIT 2000
                """,
                (user_id, kind),
            )
        else:
            rows = await run_blocking(
                self.store._query,  # noqa: SLF001
                """
                SELECT id, text, metadata, embedding FROM vectors
                WHERE user_id = ? ORDER BY id DESC LIMIT 2000
                """,
                (user_id,),
            )
        if not rows:
            return []
        query_vector = (await self.embedder.embed([query]))[0]
        scored: list[MemoryItem] = []
        for row in rows:
            try:
                vector = json.loads(row["embedding"])
            except json.JSONDecodeError:
                continue
            score = cosine(query_vector, vector)
            if score >= min_score:
                scored.append(
                    MemoryItem(
                        id=str(row["id"]),
                        text=row["text"],
                        score=score,
                        metadata=json.loads(row["metadata"] or "{}"),
                    )
                )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    async def delete_user(self, user_id: int) -> None:
        await self.init()
        await run_blocking(
            self.store._exec,  # noqa: SLF001
            "DELETE FROM vectors WHERE user_id = ?",
            (user_id,),
        )

    async def count(self, user_id: int) -> int:
        await self.init()
        row = await run_blocking(
            self.store._query_one,  # noqa: SLF001
            "SELECT COUNT(*) AS c FROM vectors WHERE user_id = ?",
            (user_id,),
        )
        return int(row["c"]) if row else 0


class ChromaVectorStore:
    backend = "chroma"

    def __init__(self, embedder: Embedder, host: str = "", port: int = 8000, path: str = ""):
        import chromadb  # type: ignore
        from chromadb.config import Settings as ChromaSettings  # type: ignore

        self.embedder = embedder
        if host:
            self.client = chromadb.HttpClient(
                host=host, port=port, settings=ChromaSettings(anonymized_telemetry=False)
            )
        else:
            self.client = chromadb.PersistentClient(
                path=path or "./data/chroma",
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        self._collections: dict[str, Any] = {}

    def _collection(self, user_id: int):
        name = f"user_{user_id}"
        if name not in self._collections:
            self._collections[name] = self.client.get_or_create_collection(
                name=name, metadata={"hnsw:space": "cosine"}
            )
        return self._collections[name]

    async def init(self) -> None:
        return None

    async def add(
        self, user_id: int, kind: str, text: str, metadata: dict | None = None
    ) -> str:
        vector = (await self.embedder.embed([text]))[0]
        meta = dict(metadata or {})
        meta["kind"] = kind
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
        meta = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
        collection = self._collection(user_id)

        def _add() -> str:
            import uuid

            doc_id = str(uuid.uuid4())
            collection.add(ids=[doc_id], documents=[text], embeddings=[vector], metadatas=[meta])
            return doc_id

        return await run_blocking(_add)

    async def search(
        self,
        user_id: int,
        query: str,
        *,
        kind: str | None = None,
        top_k: int = 6,
        min_score: float = 0.05,
    ) -> list[MemoryItem]:
        vector = (await self.embedder.embed([query]))[0]
        collection = self._collection(user_id)
        where = {"kind": kind} if kind else None

        def _query():
            return collection.query(
                query_embeddings=[vector],
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )

        try:
            result = await run_blocking(_query)
        except Exception as exc:  # noqa: BLE001
            log.warning("chroma query failed: %s", exc)
            return []
        items: list[MemoryItem] = []
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        ids = (result.get("ids") or [[]])[0]
        for idx, doc in enumerate(docs):
            distance = dists[idx] if idx < len(dists) else 1.0
            score = 1.0 - float(distance)  # cosine distance -> similarity
            if score < min_score:
                continue
            items.append(
                MemoryItem(
                    id=str(ids[idx]) if idx < len(ids) else str(idx),
                    text=doc,
                    score=score,
                    metadata=dict(metas[idx]) if idx < len(metas) else {},
                )
            )
        return items

    async def delete_user(self, user_id: int) -> None:
        try:
            self.client.delete_collection(f"user_{user_id}")
            self._collections.pop(f"user_{user_id}", None)
        except Exception as exc:  # noqa: BLE001
            log.warning("chroma delete failed: %s", exc)

    async def count(self, user_id: int) -> int:
        try:
            return int(self._collection(user_id).count())
        except Exception:
            return 0


def build_vector_store(settings, store, embedder) -> SQLiteVectorStore | ChromaVectorStore:
    try:
        return ChromaVectorStore(
            embedder,
            host=settings.chroma_host,
            port=settings.chroma_port,
            path=str(settings.memory_path / "chroma"),
        )
    except Exception as exc:  # noqa: BLE001
        log.info("Chroma unavailable (%s); using SQLite vector store", exc)
        return SQLiteVectorStore(store, embedder)
