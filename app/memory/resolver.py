"""Reference resolution for "perbaiki yang tadi", "lanjutkan itu", "yang kemarin".

The naive approach (just take the last message) is exactly what we must avoid:
the last message is often "oke", "lanjut", or the correction itself. Instead we
run a small, explicit pipeline:

1. Detect that the message *is* a reference (regex for Indonesian/English
   follow-up words) or that it carries an explicit target ("video yang tadi").
2. If it names a kind, take the most recent artifact of that kind.
3. Otherwise ask a small model to pick the best candidate from a compact list
   of (recent artifacts + recent topics). Only titles/metadata go to the model,
   never the full transcript, so the call stays cheap.
4. Fall back deterministically: most recent artifact, then most recent topic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.logging_setup import get_logger
from app.memory.artifacts import Artifact, ArtifactRegistry, KIND_ALIASES

log = get_logger(__name__)

REFERENCE_PATTERNS = [
    r"\byang\s+tadi\b",
    r"\byg\s+tadi\b",
    r"\btadi\b",
    r"\byang\s+barusan\b",
    r"\bbarusan\b",
    r"\byang\s+kemarin\b",
    r"\bkemarin\b",
    r"\byang\s+sebelumnya\b",
    r"\bsebelumnya\b",
    r"\blanjutkan\b",
    r"\blanjut\b",
    r"\bperbaiki\b",
    r"\bfix\b",
    r"\bedit\b",
    r"\brevisi\b",
    r"\bubah\b",
    r"\btambahkan\b",
    r"\bupdate\b",
    r"\bit\b",
    r"\bthat\s+one\b",
    r"\bthe\s+last\s+one\b",
    r"\bcontinue\b",
    r"\byang\s+sama\b",
    r"\bsama\s+seperti\s+tadi\b",
]

_KIND_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(gambar|image|foto|pic|poster|logo)\b", re.I), "image"),
    (re.compile(r"\b(video|film|animasi|manim|klip)\b", re.I), "video"),
    (re.compile(r"\b(suara|audio|voice|lagu|musik|tts|dubbing)\b", re.I), "audio"),
    (re.compile(r"\b(web\s*app|fullstack|login|database|supabase)\b", re.I), "web_app"),
    (re.compile(r"\b(website|web|situs|landing\s*page|html)\b", re.I), "web"),
    (re.compile(r"\b(n8n|workflow|otomasi)\b", re.I), "n8n"),
    (re.compile(r"\b(link|youtube|tiktok|instagram|ig|reels)\b", re.I), "link"),
    (re.compile(r"\b(riset|research|belajar|pelajari)\b", re.I), "research"),
]

_TEMPORAL_HINTS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\bkemarin\b", re.I), 2),
    (re.compile(r"\bminggu\s+lalu\b", re.I), 7),
    (re.compile(r"\bbulan\s+lalu\b", re.I), 30),
]


@dataclass
class Reference:
    """What the user is pointing at."""

    is_reference: bool
    artifact: Artifact | None = None
    topic: str = ""
    kind: str = ""
    confidence: float = 0.0
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.artifact is not None or bool(self.topic)

    @property
    def needs_clarification(self) -> bool:
        """Referenced a kind we have no artifact for, and nothing else matched."""
        return self.is_reference and not self.resolved


def looks_like_reference(text: str) -> bool:
    lowered = text.lower().strip()
    if len(lowered) > 220:  # a long message is a new request, not a pointer
        return False
    return any(re.search(pattern, lowered) for pattern in REFERENCE_PATTERNS)


def explicit_kind(text: str) -> str:
    for pattern, kind in _KIND_HINTS:
        if pattern.search(text):
            return kind
    return ""


def temporal_window_days(text: str) -> int:
    for pattern, days in _TEMPORAL_HINTS:
        if pattern.search(text):
            return days
    return 0


class ReferenceResolver:
    def __init__(self, registry: ArtifactRegistry, store, gateway=None):
        self.registry = registry
        self.store = store
        self.gateway = gateway

    async def resolve(self, user_id: int, text: str, *, use_llm: bool = True) -> Reference:
        if not looks_like_reference(text):
            return Reference(is_reference=False)

        kind = explicit_kind(text)
        window = temporal_window_days(text)

        # 1. Explicit kind -> newest artifact of that kind (respecting "kemarin").
        if kind:
            candidates = await self._candidates(user_id, kind=kind, window_days=window)
            if candidates:
                return Reference(
                    is_reference=True,
                    artifact=candidates[0],
                    kind=kind,
                    confidence=0.9,
                    reason=f"kind={kind}",
                )
            # The user named a kind we have never produced. Do NOT fall through to
            # "newest artifact overall" - that would silently edit the wrong thing.
            return Reference(
                is_reference=True,
                kind=kind,
                confidence=0.3,
                reason=f"kind-miss={kind}",
            )

        # 2. Named keyword that matches an artifact title.
        keyword = self._keyword(text)
        if keyword:
            found = await self.registry.find(user_id, keyword, limit=3)
            if found:
                return Reference(
                    is_reference=True,
                    artifact=found[0],
                    kind=found[0].kind,
                    confidence=0.85,
                    reason=f"keyword={keyword}",
                )

        # 3. LLM disambiguation over a compact candidate list.
        candidates = await self._candidates(user_id, window_days=window, limit=12)
        if use_llm and self.gateway and len(candidates) > 1:
            choice = await self._llm_pick(user_id, text, candidates)
            if choice is not None:
                return Reference(
                    is_reference=True,
                    artifact=candidates[choice],
                    kind=candidates[choice].kind,
                    confidence=0.75,
                    reason="llm",
                )

        # 4. Deterministic fallback: newest artifact overall, else newest topic.
        if candidates:
            return Reference(
                is_reference=True,
                artifact=candidates[0],
                kind=candidates[0].kind,
                confidence=0.55,
                reason="recency",
            )

        topic = await self._recent_topic(user_id, window)
        if topic:
            return Reference(is_reference=True, topic=topic, confidence=0.4, reason="topic")
        return Reference(is_reference=False)

    # ------------------------------------------------------------------
    async def _candidates(
        self, user_id: int, *, kind: str | None = None, window_days: int = 0, limit: int = 12
    ) -> list[Artifact]:
        artifacts = await self.registry.list(user_id, limit=60)
        if kind:
            artifacts = [a for a in artifacts if a.kind == kind]
        if window_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
            filtered = []
            for artifact in artifacts:
                try:
                    created = datetime.fromisoformat(artifact.created_at)
                except ValueError:
                    filtered.append(artifact)
                    continue
                if created >= cutoff:
                    filtered.append(artifact)
            artifacts = filtered or artifacts
        return artifacts[:limit]

    async def _recent_topic(self, user_id: int, window_days: int = 0) -> str:
        messages = await self.store.recent_messages(user_id, limit=30)
        for message in reversed(messages):
            if message["role"] != "user":
                continue
            content = message["content"].strip()
            if len(content) < 12 or looks_like_reference(content):
                continue
            return content[:200]
        return ""

    async def _llm_pick(
        self, user_id: int, text: str, candidates: list[Artifact]
    ) -> int | None:
        if not candidates:
            return None
        lines = []
        for idx, artifact in enumerate(candidates):
            meta = artifact.meta or {}
            hint = meta.get("prompt") or meta.get("topic") or meta.get("title") or ""
            lines.append(
                f"{idx}. kind={artifact.kind} title={artifact.title or '-'} "
                f"when={artifact.created_at[:16]} note={str(hint)[:90]}"
            )
        prompt = (
            "User menulis pesan lanjutan yang merujuk ke pekerjaan sebelumnya.\n"
            f"Pesan: \"{text[:300]}\"\n\n"
            "Kandidat pekerjaan terakhir (terbaru dulu):\n" + "\n".join(lines) + "\n\n"
            "Pilih satu nomor kandidat yang paling mungkin dimaksud user. "
            'Balas JSON: {"index": <nomor>} atau {"index": -1} kalau tidak ada yang cocok.'
        )
        try:
            result = await self.gateway.complete_json(
                [
                    {
                        "role": "system",
                        "content": "Kamu resolver referensi. Jawab JSON saja, tanpa penjelasan.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tier="small",
                user_id=user_id,
                temperature=0.0,
                max_tokens=60,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("resolver llm failed: %s", exc)
            return None
        index = result.get("index")
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(candidates):
            return index
        return None

    @staticmethod
    def _keyword(text: str) -> str:
        quoted = re.findall(r"[\"'“”]([^\"'“”]{3,40})[\"'“”]", text)
        if quoted:
            return quoted[0]
        # e.g. "perbaiki website portofolio" -> "portofolio"
        tokens = [
            token
            for token in re.findall(r"[a-zA-Z0-9_\-]{4,}", text.lower())
            if token
            not in {
                "perbaiki",
                "lanjutkan",
                "lanjut",
                "tadi",
                "yang",
                "kemarin",
                "sebelumnya",
                "revisi",
                "ubah",
                "tambahkan",
                "update",
                "edit",
                "video",
                "gambar",
                "audio",
                "website",
                "sama",
                "seperti",
            }
        ]
        return tokens[0] if tokens else ""


def kind_from_alias(word: str) -> str:
    return KIND_ALIASES.get(word.strip().lower(), "")
