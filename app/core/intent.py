"""Intent routing.

Two stages, cheapest first:

1. Fast path: deterministic rules for commands, URLs, media attachments and
   obvious imperatives. Handles the majority of traffic with zero tokens.
2. Slow path: one small-model JSON classification, used only when the rules are
   not confident. The classifier sees a short message and a fixed label list,
   nothing else.

Every route carries the args the handler needs, so handlers never re-parse text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.core.logging_setup import get_logger

log = get_logger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
YT_RE = re.compile(
    r"(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|"
    r"twitter\.com|x\.com|vimeo\.com|dailymotion\.com|twitch\.tv)",
    re.I,
)


class Intent(str, Enum):
    CHAT = "chat"
    THINK = "think"
    IMAGE_CREATE = "image_create"
    IMAGE_EDIT = "image_edit"
    VIDEO_CREATE = "video_create"
    VIDEO_EDIT = "video_edit"
    AUDIO_CREATE = "audio_create"
    AUDIO_EDIT = "audio_edit"
    TTS = "tts"
    STT = "stt"
    WEB_STATIC = "web_static"
    WEB_FULLSTACK = "web_fullstack"
    WEB_REPAIR = "web_repair"
    N8N = "n8n"
    LEARN = "learn"
    LINK_INGEST = "link_ingest"
    REMINDER = "reminder"
    MEMORY_QUERY = "memory_query"
    FEEDBACK = "feedback"
    HELP = "help"
    UNKNOWN = "unknown"


HEAVY_INTENTS = {
    Intent.IMAGE_CREATE,
    Intent.IMAGE_EDIT,
    Intent.VIDEO_CREATE,
    Intent.VIDEO_EDIT,
    Intent.AUDIO_CREATE,
    Intent.AUDIO_EDIT,
    Intent.WEB_STATIC,
    Intent.WEB_FULLSTACK,
    Intent.WEB_REPAIR,
    Intent.N8N,
    Intent.LINK_INGEST,
    Intent.LEARN,
    Intent.THINK,
}


@dataclass
class Route:
    intent: Intent
    args: dict = field(default_factory=dict)
    confidence: float = 1.0
    source: str = "rule"
    needs_confirm: bool = False
    estimate_tokens: int = 0

    def __post_init__(self) -> None:
        if self.intent in HEAVY_INTENTS and not self.needs_confirm:
            self.needs_confirm = True


# ---------------------------------------------------------------- fast rules
_RULES: list[tuple[re.Pattern[str], Intent, dict]] = [
    (re.compile(r"^\s*/?(bantu|help|menu|perintah)\b", re.I), Intent.HELP, {}),
    (re.compile(r"^\s*/?(pikir|think|analisa|analisis mendalam)\b", re.I), Intent.THINK, {}),
    (
        re.compile(r"^\s*/?(belajar|pelajari|riset|research|search|cari tahu)\b", re.I),
        Intent.LEARN,
        {},
    ),
    (
        re.compile(r"^\s*/?(ingat|remember|catat)\b", re.I),
        Intent.MEMORY_QUERY,
        {"mode": "write"},
    ),
    (
        re.compile(r"^\s*/?(lupa|forget|hapus memori)\b", re.I),
        Intent.MEMORY_QUERY,
        {"mode": "forget"},
    ),
    (
        re.compile(r"^\s*/?(memori|memory|statistik|status|budget)\b", re.I),
        Intent.MEMORY_QUERY,
        {"mode": "stats"},
    ),
    (
        re.compile(r"^\s*/?(reminder|ingatkan|jadwalkan|jadwal)\b", re.I),
        Intent.REMINDER,
        {},
    ),
    (
        re.compile(r"^\s*/?(feedback|saran|kritik)\b", re.I),
        Intent.FEEDBACK,
        {},
    ),
    (
        re.compile(
            r"\b(buat(kan)?|bikin|bikinin|generate|gambar(kan)?|desain|lukis|render)\b.{0,40}"
            r"\b(gambar|foto|image|logo|poster|ilustrasi|wallpaper|icon|thumbnail)\b",
            re.I,
        ),
        Intent.IMAGE_CREATE,
        {},
    ),
    (
        re.compile(
            r"\b(gambar|foto|image|logo|poster|ilustrasi|wallpaper)\b.{0,30}"
            r"\b(buat|bikin|generate|desain)\b",
            re.I,
        ),
        Intent.IMAGE_CREATE,
        {},
    ),
    (
        re.compile(
            r"\b(edit|ubah|perbaiki|ganti|modif|tambah(kan)?)\b.{0,40}"
            r"\b(gambar|foto|image|logo|poster)\b",
            re.I,
        ),
        Intent.IMAGE_EDIT,
        {},
    ),
    (
        re.compile(
            r"\b(buat(kan)?|bikin|generate|render|produksi)\b.{0,40}"
            r"\b(video|animasi|manim|klip|motion|reels)\b",
            re.I,
        ),
        Intent.VIDEO_CREATE,
        {},
    ),
    (
        re.compile(r"\b(edit|potong|gabung|subtitle|speed|perbaiki)\b.{0,40}\b(video|klip)\b", re.I),
        Intent.VIDEO_EDIT,
        {},
    ),
    (
        re.compile(
            r"\b(buat(kan)?|bikin|generate|komposisi|produksi)\b.{0,40}"
            r"\b(suara|audio|musik|lagu|voice|voiceover|dubbing|jingle)\b",
            re.I,
        ),
        Intent.AUDIO_CREATE,
        {},
    ),
    (
        re.compile(r"\b(edit|potong|gabung|perbaiki|ubah)\b.{0,40}\b(suara|audio|voice)\b", re.I),
        Intent.AUDIO_EDIT,
        {},
    ),
    (
        re.compile(r"\b(bacakan|ucapkan|tts|text to speech|jadiin suara|bikin voice)\b", re.I),
        Intent.TTS,
        {},
    ),
    (
        re.compile(r"\b(transkrip|transkripsi|stt|speech to text|ubah ke teks)\b", re.I),
        Intent.STT,
        {},
    ),
    (
        re.compile(
            r"\b(web\s*app|fullstack|full\s*stack|login|signup|auth|database|crud|dashboard)\b",
            re.I,
        ),
        Intent.WEB_FULLSTACK,
        {},
    ),
    (
        re.compile(r"\b(perbaiki|repair|debug|fix)\b.{0,30}\b(web|website|situs|deploy)\b", re.I),
        Intent.WEB_REPAIR,
        {},
    ),
    (
        re.compile(
            r"\b(buat(kan)?|bikin(kan)?|generate|bikinin)\b.{0,40}"
            r"\b(web|website|situs|landing\s*page|portofolio|company\s*profile)\b",
            re.I,
        ),
        Intent.WEB_STATIC,
        {},
    ),
    (
        re.compile(r"\b(n8n|workflow|otomasi|automation|integrasi)\b", re.I),
        Intent.N8N,
        {},
    ),
]


def classify_rules(text: str, *, has_photo: bool = False, has_voice: bool = False) -> Route | None:
    stripped = text.strip()

    urls = URL_RE.findall(stripped)
    if urls and (YT_RE.search(stripped) or len(stripped) < 300):
        return Route(
            Intent.LINK_INGEST,
            {"urls": urls, "question": _strip_urls(stripped)},
            confidence=0.95,
        )

    if has_photo:
        if re.search(r"\b(edit|ubah|perbaiki|ganti|modif|tambahkan?)\b", stripped, re.I):
            return Route(Intent.IMAGE_EDIT, {"instruction": stripped}, confidence=0.85)
        return Route(Intent.IMAGE_EDIT, {"instruction": stripped or "perbaiki gambar ini"}, 0.6)

    if has_voice:
        if re.search(r"\b(transkrip|teks|stt)\b", stripped, re.I):
            return Route(Intent.STT, {}, confidence=0.9)
        return Route(Intent.STT, {"question": stripped}, confidence=0.7)

    if stripped.startswith("/"):
        return None  # explicit commands are dispatched by the command handlers

    for pattern, intent, extra in _RULES:
        match = pattern.search(stripped)
        if match:
            args = dict(extra)
            args.setdefault("text", stripped)
            return Route(intent, args, confidence=0.8)

    if len(stripped) < 4:
        return Route(Intent.CHAT, {"text": stripped}, confidence=0.5)
    return None


def _strip_urls(text: str) -> str:
    cleaned = URL_RE.sub("", text).strip()
    return cleaned or "Jelaskan isi link ini."


CLASSIFIER_LABELS = [
    "chat",
    "think",
    "image_create",
    "image_edit",
    "video_create",
    "video_edit",
    "audio_create",
    "audio_edit",
    "tts",
    "web_static",
    "web_fullstack",
    "web_repair",
    "n8n",
    "learn",
    "link_ingest",
    "reminder",
]

CLASSIFIER_PROMPT = """Klasifikasikan pesan user ke SATU label.

Label:
- chat: ngobrol, tanya jawab, minta pendapat, brainstorming ringan
- think: minta analisis mendalam / reasoning bertahap
- image_create: minta dibuatkan gambar/logo/poster
- image_edit: minta gambar yang SUDAH ADA diubah/diperbaiki
- video_create: minta dibuatkan video/animasi baru
- video_edit: minta video yang sudah ada dipotong/digabung/diedit
- audio_create: minta dibuatkan musik/suara/jingle baru
- audio_edit: minta audio yang sudah ada diedit
- tts: minta teks dibacakan jadi suara
- web_static: minta website statis/landing page/portofolio
- web_fullstack: minta web dengan login/database/CRUD
- web_repair: minta website yang sudah dideploy diperbaiki
- n8n: minta workflow otomasi n8n
- learn: minta belajar/riset topik lalu disimpan
- link_ingest: mengirim link video/artikel untuk dibahas
- reminder: minta pengingat/jadwal

Balas HANYA JSON: {"intent": "<label>", "confidence": <0..1>, "reason": "<max 12 kata>"}"""


async def classify_llm(text: str, gateway, user_id: int = 0) -> Route:
    """Small-model classification, used only when rules are unsure."""
    try:
        data = await gateway.complete_json(
            [
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": text[:1200]},
            ],
            tier="small",
            user_id=user_id,
            temperature=0.0,
            max_tokens=120,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("intent classification failed: %s", exc)
        return Route(Intent.CHAT, {"text": text}, confidence=0.2, source="llm-error")

    label = str(data.get("intent", "chat")).strip().lower()
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    if label not in CLASSIFIER_LABELS:
        label = "chat"
    intent = Intent(label)
    return Route(
        intent,
        {"text": text, "reason": str(data.get("reason", ""))[:80]},
        confidence=max(0.0, min(1.0, confidence)),
        source="llm",
    )


async def route_message(
    text: str,
    gateway=None,
    *,
    user_id: int = 0,
    has_photo: bool = False,
    has_voice: bool = False,
    has_document: bool = False,
) -> Route:
    rule = classify_rules(text, has_photo=has_photo, has_voice=has_voice)
    if rule is not None:
        return rule
    if gateway is not None and gateway.available():
        return await classify_llm(text, gateway, user_id=user_id)
    return Route(Intent.CHAT, {"text": text}, confidence=0.3, source="fallback")
