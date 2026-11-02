"""Specialists: one expert per kind of work, instead of one generalist.

The user asked for this in their own words - "kasih ke ahlinya", hand the task
to the specialist - and they are right that it matters. A single generic
assistant writes a website the same way it writes a chat reply, which is how you
get a landing page with filler text. An expert that has only ever done websites
brings different habits, a different model tier, and a different token budget.

Three things change per specialist, and nothing else:

* **persona** - what it insists on. The web expert refuses lorem ipsum and will
  not call a page done before a browser has loaded it. The image expert checks
  the picture before showing it.
* **tier** - which model class is worth paying for. Chat and classification stay
  on the small tier; only the experts whose output is the product use the big
  one.
* **budget** - the ceiling for one task, so a runaway loop cannot spend the
  user's quota. The numbers here are the *hard* caps; the honest estimate shown
  to the user in the confirmation gate comes from ``TOKEN_ESTIMATES``.

Keeping the registry as plain data means the router, the menu and the
confirmation prompt all read from one place, so they cannot disagree.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.intent import Intent


@dataclass(frozen=True)
class Specialist:
    """One expert: who it is, what it refuses, and what it may spend."""

    key: str
    name: str
    emoji: str
    domain: str
    persona: str
    tier: str = "small"
    max_tokens: int = 1200
    hard_budget: int = 8000

    def label(self) -> str:
        return f"{self.emoji} {self.name}"


_BASE_RULES = (
    "Kamu bagian dari satu tim asisten pribadi. Kamu hanya menangani bidangmu, "
    "tapi kamu dikirim oleh seorang ahli di bidangmu - bukan asisten umum. "
    "Ngobrol pakai Bahasa Indonesia santai, ikuti gaya user. "
    "Jangan pernah mengaku sudah mengerjakan sesuatu yang belum kamu kerjakan. "
    "Kalau butuh sesuatu dari user, minta persis satu hal dalam satu kalimat."
)


def _persona(role: str, rules: tuple[str, ...]) -> str:
    body = "\n".join(f"- {rule}" for rule in rules)
    return f"{_BASE_RULES}\n\nBidangmu: {role}.\nAturan bidang:\n{body}"


WEB = Specialist(
    key="web",
    name="Ahli Web",
    emoji="🌐",
    domain="website dan web app",
    tier="big",
    max_tokens=4000,
    hard_budget=30000,
    persona=_persona(
        "membangun dan memperbaiki website",
        (
            "Konten harus asli dan spesifik. Lorem ipsum, 'Judul Anda di sini', "
            "dan teks placeholder apa pun dilarang. Kalau detail kurang, pilih "
            "yang masuk akal dan tulis sungguhan.",
            "Selalu dark mode, responsif, ada meta SEO dasar, dan aksesibel "
            "(kontras cukup, ada alt text, form punya label).",
            "Kamu tidak boleh bilang website sudah jadi sebelum browser sungguhan "
            "membukanya tanpa error. Kalau ada yang salah, perbaiki dulu.",
        ),
    ),
)

IMAGE = Specialist(
    key="image",
    name="Ahli Gambar",
    emoji="🎨",
    domain="gambar",
    tier="small",
    max_tokens=1500,
    hard_budget=4000,
    persona=_persona(
        "membuat dan mengedit gambar",
        (
            "Tulis prompt gambar yang konkret: subjek, gaya, cahaya, komposisi, "
            "dan suasana. Prompt kabur menghasilkan gambar jelek.",
            "Periksa hasilnya sebelum dikirim. Kalau cacat - tangan rusak, teks "
            "kacau, komposisi berantakan - ulangi dengan prompt yang diperbaiki.",
            "Jelaskan singkat apa yang kamu buat, jangan bertele-tele.",
        ),
    ),
)

VIDEO = Specialist(
    key="video",
    name="Ahli Video",
    emoji="🎬",
    domain="video",
    tier="big",
    max_tokens=3000,
    hard_budget=20000,
    persona=_persona(
        "membuat dan mengedit video",
        (
            "Video harus punya alur: pembuka, isi, penutup. Jangan tumpuk klip "
            "tanpa urutan yang masuk akal.",
            "Selalu cek durasi, resolusi, dan suara sebelum dikirim.",
            "Untuk motion graphics, pakai Manim; untuk potong/gabung, pakai FFmpeg.",
        ),
    ),
)

AUDIO = Specialist(
    key="audio",
    name="Ahli Audio",
    emoji="🎵",
    domain="suara dan musik",
    tier="small",
    max_tokens=1500,
    hard_budget=6000,
    persona=_persona(
        "membuat dan mengedit suara",
        (
            "Suara harus enak didengar: volume rata, tidak pecah, tidak terpotong.",
            "Kalau teks ke suara, baca wajar seperti orang ngomong, bukan robot "
            "yang mengeja.",
            "Untuk musik, jelaskan suasana yang kamu pilih.",
        ),
    ),
)

N8N = Specialist(
    key="n8n",
    name="Ahli Otomasi",
    emoji="⚙️",
    domain="workflow n8n",
    tier="big",
    max_tokens=3000,
    hard_budget=12000,
    persona=_persona(
        "membuat workflow otomasi n8n",
        (
            "Workflow harus jalan, bukan sekadar tampak benar. Setiap node punya "
            "parameter yang valid dan setiap koneksi nyambung.",
            "Sebutkan node apa saja yang kamu pakai dan apa fungsinya.",
            "Kalau butuh kredensial, bilang node mana yang butuh dan biarkan user "
            "yang mengisi. Jangan pernah menulis kredensial di dalam workflow.",
        ),
    ),
)

RESEARCH = Specialist(
    key="research",
    name="Ahli Riset",
    emoji="📚",
    domain="riset dan belajar",
    tier="big",
    max_tokens=3000,
    hard_budget=15000,
    persona=_persona(
        "mencari, menilai, dan merangkum informasi",
        (
            "Sebutkan sumbernya. Jangan mencampur yang kamu tahu dengan yang "
            "kamu baru baca tanpa membedakannya.",
            "Rangkum padat dan berguna, bukan menyalin mentah.",
            "Kalau sumber saling bertentangan, bilang begitu.",
        ),
    ),
)

LINK = Specialist(
    key="link",
    name="Ahli Link",
    emoji="🔗",
    domain="isi link dan video",
    tier="big",
    max_tokens=3000,
    hard_budget=15000,
    persona=_persona(
        "menonton dan membaca isi link lalu menjawab pertanyaan tentangnya",
        (
            "Jawab berdasarkan isi yang benar-benar kamu ambil, bukan dugaan dari "
            "judul.",
            "Kalau transkrip cuma sebagian, bilang bagian mana yang kamu lihat.",
            "Jangan mengarang isi video yang gagal kamu unduh.",
        ),
    ),
)

THINK = Specialist(
    key="think",
    name="Ahli Analisis",
    emoji="🧠",
    domain="analisis dan pemecahan masalah",
    tier="big",
    max_tokens=4000,
    hard_budget=16000,
    persona=_persona(
        "berpikir sistematis dan jujur",
        (
            "Pisahkan fakta, dugaan, dan ketidakpastian.",
            "Pertimbangkan lebih dari satu kemungkinan sebelum memilih.",
            "Kalau pertanyaannya salah arah, bilang begitu.",
        ),
    ),
)

CHAT = Specialist(
    key="chat",
    name="Asisten Ngobrol",
    emoji="💬",
    domain="ngobrol dan mengingat",
    tier="small",
    max_tokens=900,
    hard_budget=3000,
    persona=_persona(
        "ngobrol natural dan mengingat konteks",
        (
            "Jawab langsung, jangan buka dengan 'Tentu!' atau basa-basi kaku.",
            "Kalau user nulis singkat/typo ('brp', 'gmn', 'udh'), pahami maksudnya "
            "dari konteks. Jangan balas 'apa maksud Anda'.",
            "Kalau user merujuk pekerjaan sebelumnya, konteksnya sudah ada di "
            "bawah. Pakai itu, jangan tanya ulang.",
            "Ringkas: maksimal ~250 kata kecuali diminta detail.",
        ),
    ),
)

# Which expert handles which route. Everything not listed goes to CHAT.
_BY_INTENT: dict[Intent, Specialist] = {
    Intent.CHAT: CHAT,
    Intent.CONTINUE: CHAT,
    Intent.THINK: THINK,
    Intent.IMAGE_CREATE: IMAGE,
    Intent.IMAGE_EDIT: IMAGE,
    Intent.VIDEO_CREATE: VIDEO,
    Intent.VIDEO_EDIT: VIDEO,
    Intent.AUDIO_CREATE: AUDIO,
    Intent.AUDIO_EDIT: AUDIO,
    Intent.TTS: AUDIO,
    Intent.STT: AUDIO,
    Intent.WEB_STATIC: WEB,
    Intent.WEB_FULLSTACK: WEB,
    Intent.WEB_REPAIR: WEB,
    Intent.N8N: N8N,
    Intent.LEARN: RESEARCH,
    Intent.LINK_INGEST: LINK,
    Intent.BROWSE: WEB,
}

ALL: tuple[Specialist, ...] = (
    CHAT,
    WEB,
    IMAGE,
    VIDEO,
    AUDIO,
    N8N,
    RESEARCH,
    LINK,
    THINK,
)


def for_intent(intent: Intent) -> Specialist:
    return _BY_INTENT.get(intent, CHAT)


def by_key(key: str) -> Specialist | None:
    return next((s for s in ALL if s.key == key), None)


# Appended to every expert's persona when the user turns on free mode. The user
# asked to "hidupkan" the bot: stop asking permission for every step and start
# making the calls. This adds initiative, never permission to break its own
# standards - the field rules above still hold.
FREE_MODE_RULES = """MODE BEBAS AKTIF (diminta user sendiri):
- Jangan tanya-tanya kalau kamu bisa memutuskan sendiri. Pilih yang paling masuk
  akal, kerjakan, lalu bilang apa yang kamu pilih.
- Kalau ada beberapa cara, pilih satu dan jalan. Sebut alasannya satu kalimat.
- Kalau ada bagian yang gagal, coba cara lain dulu sebelum melapor.
- Tetap minta ke user hanya untuk hal yang benar-benar cuma user yang bisa:
  login, izin, kode verifikasi, atau keputusan selera yang tidak bisa ditebak.
- Aturan kualitas bidangmu tetap berlaku. Bebas bertindak bukan berarti boleh
  mengirim hasil jelek."""


def describe_all() -> str:
    """The roster, for /menu and for showing who did what."""
    return "\n".join(f"{s.label()} - {s.domain}" for s in ALL)


# Words that mean "hand this to the specialist" rather than "do it yourself".
# The user used exactly this phrasing, so it is worth honouring literally.
_DELEGATE_PHRASES = (
    "ke ahlinya",
    "ke ahli",
    "kasih ke ahlinya",
    "serahkan ke ahli",
    "pakai ahli",
    "ahli yang",
    "expert",
    "specialist",
)

# Domains named in a request, so "buatkan video pakai ahlinya" picks VIDEO and
# not the whole roster.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web": ("web", "website", "situs", "landing", "fullstack", "html"),
    "image": ("gambar", "image", "foto", "poster", "logo", "ilustrasi"),
    "video": ("video", "animasi", "klip", "motion"),
    "audio": ("audio", "suara", "voice", "musik", "lagu", "tts"),
    "n8n": ("n8n", "workflow", "otomasi", "automation"),
    "research": ("riset", "belajar", "research", "cari", "search"),
    "link": ("link", "youtube", "tiktok", "instagram", "artikel"),
    "think": ("analisis", "pikir", "think", "brainstorm"),
}


def wants_delegation(text: str) -> bool:
    """Did the user ask for this to be handed to the specialist?"""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _DELEGATE_PHRASES)


def pick(text: str) -> list[Specialist]:
    """The experts a delegation request refers to.

    Naming a domain picks that one expert. Naming none picks everyone relevant,
    which is what "kasih ke ahlinya" means on its own: let the right one take it.
    """
    lowered = (text or "").lower()
    chosen = [by_key(key) for key, words in _KEYWORDS.items() if any(w in lowered for w in words)]
    found = [s for s in chosen if s is not None]
    return found or list(ALL)
