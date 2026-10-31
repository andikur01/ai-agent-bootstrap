"""Memory facade.

Ties together everything the bot remembers about a user:
* raw chat history (SQLite, for exact recency and keyword search)
* rolling topic summaries (vector store, ``kind="summary"``)
* learned facts (vector store, ``kind="learning"``)
* artifact registry (what we produced)
* user preferences

Crucially, ``build_context`` never dumps the whole transcript into the prompt.
It assembles a bounded, deduplicated block: recent turns + the few most
relevant long-term memories + the latest artifacts. That is the token saving.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.logging_setup import get_logger
from app.llm.tokens import count_tokens, trim_to_tokens
from app.memory.artifacts import Artifact, ArtifactRegistry
from app.memory.resolver import Reference, ReferenceResolver
from app.memory.vector_store import MemoryItem, SQLiteVectorStore

log = get_logger(__name__)

KIND_SUMMARY = "summary"
KIND_LEARNING = "learning"
KIND_FACT = "fact"


@dataclass
class ContextBundle:
    system: str
    messages: list[dict]
    used_memories: list[str]
    used_artifacts: list[str]
    reference: Reference | None = None

    @property
    def tokens(self) -> int:
        return count_tokens(self.system) + sum(count_tokens(m.get("content", "")) for m in self.messages)


class MemoryManager:
    def __init__(self, store, vectors, registry: ArtifactRegistry, settings, gateway=None):
        self.store = store
        self.vectors = vectors
        self.registry = registry
        self.settings = settings
        self.gateway = gateway
        self.resolver = ReferenceResolver(registry, store, gateway=gateway)

    # ------------------------------------------------------------------ write
    async def remember_message(
        self, user_id: int, role: str, content: str, meta: dict | None = None
    ) -> int:
        return await self.store.add_message(user_id, role, content, meta)

    async def remember_topic(
        self, user_id: int, summary: str, *, topic: str = "", source: str = "chat"
    ) -> str:
        text = f"TOPIK: {topic}\n{summary}" if topic else summary
        return await self.vectors.add(
            user_id, KIND_SUMMARY, text, {"topic": topic, "source": source}
        )

    async def remember_learning(
        self, user_id: int, topic: str, summary: str, sources: list[str] | None = None
    ) -> str:
        await self.store.add_learning(user_id, topic, summary, sources or [])
        text = f"BELAJAR [{topic}]: {summary}"
        return await self.vectors.add(
            user_id, KIND_LEARNING, text, {"topic": topic, "sources": ",".join((sources or [])[:5])}
        )

    async def remember_fact(self, user_id: int, fact: str, **meta) -> str:
        return await self.vectors.add(user_id, KIND_FACT, fact, meta)

    async def register_artifact(self, user_id: int, kind: str, **kwargs) -> Artifact:
        return await self.registry.register(user_id, kind, **kwargs)

    # ------------------------------------------------------------------ read
    async def recall(self, user_id: int, query: str, *, top_k: int | None = None) -> list[MemoryItem]:
        top_k = top_k or self.settings.memory_top_k
        return await self.vectors.search(user_id, query, top_k=top_k)

    async def learnings(self, user_id: int, query: str, top_k: int = 4) -> list[MemoryItem]:
        return await self.vectors.search(user_id, query, kind=KIND_LEARNING, top_k=top_k)

    async def resolve_reference(self, user_id: int, text: str) -> Reference:
        return await self.resolver.resolve(user_id, text, use_llm=self.gateway is not None)

    async def latest_artifact(self, user_id: int, kind: str | None = None) -> Artifact | None:
        return await self.registry.latest(user_id, kind)

    async def recent_artifacts(self, user_id: int, limit: int = 5) -> list[Artifact]:
        return await self.registry.list(user_id, limit=limit)

    # ------------------------------------------------------------------ context
    async def build_context(
        self,
        user_id: int,
        user_text: str,
        *,
        persona: str = "",
        recent_turns: int = 8,
        include_artifacts: bool = True,
        reference: Reference | None = None,
    ) -> ContextBundle:
        budget = self.settings.memory_max_context_tokens
        parts: list[str] = []
        used_memories: list[str] = []
        used_artifacts: list[str] = []

        if persona:
            parts.append(persona.strip())

        # Long-term memory relevant to *this* message.
        try:
            memories = await self.recall(user_id, user_text, top_k=self.settings.memory_top_k)
        except Exception as exc:  # noqa: BLE001
            log.warning("recall failed: %s", exc)
            memories = []
        relevant = [m for m in memories if m.score >= 0.2]
        if relevant:
            block = ["INGATAN RELEVAN:"]
            for item in relevant:
                snippet = trim_to_tokens(item.text, 160)
                block.append(f"- ({item.metadata.get('kind', 'mem')}) {snippet}")
                used_memories.append(snippet)
            parts.append("\n".join(block))

        # Explicit reference resolution ("perbaiki yang tadi").
        if reference and reference.resolved:
            if reference.artifact:
                artifact = reference.artifact
                parts.append(
                    "REFERENSI USER (yang dimaksud 'tadi'):\n"
                    f"- kind: {artifact.kind}\n"
                    f"- judul: {artifact.title or '-'}\n"
                    f"- lokasi: {artifact.location}\n"
                    f"- dibuat: {artifact.created_at[:19]}\n"
                    f"- meta: {_short_json(artifact.meta)}"
                )
                used_artifacts.append(artifact.brief())
            elif reference.topic:
                parts.append(f"REFERENSI USER: lanjutan dari topik \"{reference.topic}\"")

        if include_artifacts:
            artifacts = await self.registry.list(user_id, limit=5)
            if artifacts:
                lines = ["ARTIFACT TERAKHIR:"]
                for artifact in artifacts:
                    lines.append(f"- {artifact.kind}: {artifact.title or '-'} ({artifact.location})")
                    used_artifacts.append(artifact.brief())
                parts.append("\n".join(lines))

        prefs = await self.store.get_prefs(user_id)
        if prefs:
            parts.append("PREFERENSI USER: " + _short_json(prefs))

        system = trim_to_tokens("\n\n".join(parts), budget)

        messages: list[dict] = [{"role": "system", "content": system}]
        history = await self.store.recent_messages(user_id, limit=recent_turns * 2)
        for message in history:
            role = "assistant" if message["role"] == "assistant" else "user"
            content = message["content"]
            if message["role"] == "system":
                continue
            messages.append({"role": role, "content": trim_to_tokens(content, 800)})
        messages.append({"role": "user", "content": user_text})

        return ContextBundle(
            system=system,
            messages=messages,
            used_memories=used_memories,
            used_artifacts=used_artifacts,
            reference=reference,
        )

    async def stats(self, user_id: int) -> dict:
        return {
            "messages": await self.store.count_messages(user_id),
            "vectors": await self.vectors.count(user_id),
            "artifacts": len(await self.registry.list(user_id, limit=100)),
            "backend": getattr(self.vectors, "backend", "unknown"),
        }


def _short_json(data: dict, limit: int = 240) -> str:
    import json

    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"
