"""The public-apis catalog: find which service issues the key you need.

The request was "bot ambil API key dari repo public-apis". Worth being precise
about what that repo is, because the naive reading does not work: public-apis
contains no keys. It is a directory of ~2,300 services with the *address* where
each one issues a key, plus whether a key is required and whether the API is
reachable from a browser. Scraping it for secrets would find nothing.

What it is genuinely useful for is the step before the key: deciding *where* to
get one. "Aku butuh API cuaca" used to mean guessing a provider name; now the
bot can answer with the real services, which of them need a key, and the URL to
sign up. From there the existing harvester takes over - the user signs in once,
the bot reads the key out of the console (see ``key_harvester``).

No model call happens here. The file is a markdown table; parsing it is plain
string work, so a catalog lookup costs zero tokens. That matters because this is
a question a user asks often and casually.

The catalog is cached on disk. Fetching 335 KB from GitHub on every question
would be slow and rude to GitHub, so the file is kept for a week and refreshed
only when stale or when the fetch is asked for explicitly.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

CATALOG_URL = "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
CACHE_TTL_SECONDS = 7 * 24 * 3600

# A row looks like:
#   | [Name](https://site) | What it does | `apiKey` | Yes | Yes |
# The description may contain a pipe inside a link, so the split is on " | "
# rather than on "|" - splitting on the bare pipe shreds rows that carry a
# markdown link with a title attribute.
ROW_RE = re.compile(r"^\|\s*\[(?P<name>[^\]]+)\]\((?P<url>[^)]+)\)\s*\|(?P<rest>.+)\|\s*$")
CATEGORY_RE = re.compile(r"^###\s+(?P<name>.+?)\s*$")


@dataclass
class ApiEntry:
    name: str
    url: str
    description: str
    auth: str
    https: bool
    cors: str
    category: str = ""

    @property
    def needs_key(self) -> bool:
        """True when the service hands out a key before it will answer.

        "No" means open access; anything else - `apiKey`, `OAuth`, a custom
        header - means the user has to obtain a credential somewhere, which is
        the only case worth routing to the harvester.
        """
        return self.auth.strip().strip("`").lower() not in {"", "no"}

    def render(self) -> str:
        marks = []
        if self.needs_key:
            marks.append(f"butuh {self.auth.strip().strip('`') or 'kunci'}")
        else:
            marks.append("tanpa kunci")
        if not self.https:
            marks.append("⚠️ non-HTTPS")
        if self.cors.lower() == "no":
            marks.append("CORS tertutup (tidak bisa dari browser)")
        return f"• *{self.name}* - {self.description}\n  {self.url}\n  _{', '.join(marks)}_"


class ApiCatalog:
    """Parse and query the public-apis directory."""

    def __init__(self, cache_path: Path, *, url: str = CATALOG_URL):
        self.cache_path = Path(cache_path)
        self.url = url
        self._entries: list[ApiEntry] | None = None

    # ------------------------------------------------------------ fetching
    def cached_text(self) -> str:
        if not self.cache_path.exists():
            return ""
        return self.cache_path.read_text(encoding="utf-8", errors="ignore")

    def is_stale(self) -> bool:
        if not self.cache_path.exists():
            return True
        age = time.time() - self.cache_path.stat().st_mtime
        return age > CACHE_TTL_SECONDS

    async def refresh(self) -> bool:
        """Download the directory. Returns False when the network is unusable.

        A failure here is not fatal: the cached copy, even a stale one, is far
        better than telling the user the feature is broken because GitHub had a
        bad minute.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                text = response.text
        except Exception as exc:  # noqa: BLE001
            log.warning("public-apis fetch failed: %s", exc)
            return False
        if "| API |" not in text:
            log.warning("public-apis fetch returned unexpected content")
            return False
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(text, encoding="utf-8")
        self._entries = None
        return True

    async def ensure(self) -> None:
        """Load the catalog, fetching only when there is nothing usable."""
        if self._entries is not None:
            return
        if self.is_stale():
            await self.refresh()
        self._entries = self.parse(self.cached_text())

    # ------------------------------------------------------------ parsing
    @staticmethod
    def parse(text: str) -> list[ApiEntry]:
        entries: list[ApiEntry] = []
        category = ""
        for raw in text.splitlines():
            line = raw.rstrip()
            heading = CATEGORY_RE.match(line)
            if heading:
                category = heading.group("name").strip()
                continue
            row = ROW_RE.match(line)
            if not row:
                continue
            cells = [c.strip() for c in row.group("rest").split("|")]
            if len(cells) < 4:
                continue
            # Last three cells are Auth, HTTPS, CORS. Taking them from the end
            # keeps a description that itself contains pipes from shifting them.
            auth, https, cors = cells[-3], cells[-2], cells[-1]
            if https not in {"Yes", "No", "`HTTP`"} and cors not in {"Yes", "No", "`HTTP`", "stdio"}:
                continue
            entries.append(
                ApiEntry(
                    name=row.group("name").strip(),
                    url=row.group("url").strip(),
                    description=cells[0],
                    auth=auth,
                    https=https == "Yes",
                    cors=cors,
                    category=category,
                )
            )
        return entries

    # ------------------------------------------------------------ querying
    async def search(
        self,
        query: str = "",
        *,
        category: str = "",
        needs_key: bool | None = None,
        https_only: bool = False,
        limit: int = 8,
    ) -> list[ApiEntry]:
        await self.ensure()
        subject = _normalise_subject(query)
        terms = [t for t in re.split(r"\W+", subject) if len(t) > 2]
        scored: list[tuple[int, ApiEntry]] = []
        for entry in self._entries or []:
            if category and category.lower() not in entry.category.lower():
                continue
            if needs_key is not None and entry.needs_key is not needs_key:
                continue
            if https_only and not entry.https:
                continue
            if not terms:
                scored.append((0, entry))
                continue
            haystack = f"{entry.name} {entry.description} {entry.category}".lower()
            score = sum(3 if term in entry.name.lower() else 1 for term in terms if term in haystack)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name.lower()))
        return [entry for _, entry in scored[:limit]]

    async def categories(self) -> list[str]:
        await self.ensure()
        seen: list[str] = []
        for entry in self._entries or []:
            if entry.category and entry.category not in seen:
                seen.append(entry.category)
        return seen

    async def count(self) -> int:
        await self.ensure()
        return len(self._entries or [])


# The directory is written in English; the user types Indonesian. Without this
# map, "cari api cuaca" finds nothing at all and the feature looks broken. Only
# common subjects are listed - an unknown word still works, it just searches in
# English, and the reply names the categories so the user can retry.
SUBJECT_SYNONYMS: dict[str, str] = {
    "cuaca": "weather",
    "prakiraan": "weather",
    "berita": "news",
    "kabar": "news",
    "uang": "finance currency",
    "keuangan": "finance",
    "saham": "stock finance",
    "kripto": "crypto cryptocurrency",
    "crypto": "crypto cryptocurrency",
    "musik": "music",
    "lagu": "music",
    "film": "movie movies",
    "video": "video",
    "gambar": "image photo",
    "foto": "photo image",
    "peta": "geocoding maps",
    "lokasi": "geocoding",
    "alamat": "geocoding",
    "terjemah": "translation translate",
    "terjemahan": "translation translate",
    "bahasa": "translation language",
    "kamus": "dictionary",
    "resep": "food recipe",
    "makanan": "food recipe",
    "kesehatan": "health",
    "olahraga": "sports",
    "sepakbola": "sports football",
    "buku": "books",
    "pendidikan": "education",
    "sekolah": "education",
    "ai": "machine learning artificial intelligence",
    "kecerdasan": "machine learning",
    "data": "data",
    "email": "email",
    "sms": "sms phone",
    "telepon": "phone",
    "quran": "religion",
    "agama": "religion",
    "sholat": "religion",
    "penerbangan": "aviation flight",
    "kereta": "transport",
    "kendaraan": "vehicle transport",
    "game": "games",
    "hiburan": "entertainment",
    "anime": "anime",
    "manga": "anime",
    "hewan": "animals",
    "kucing": "animals cats",
    "anjing": "animals dogs",
    "tumbuhan": "plants",
    "astronomi": "space astronomy",
    "luar": "space",
    "sejarah": "history",
    "hukum": "legal",
    "pemerintah": "government",
    "pajak": "government",
    "sosial": "social",
    "kecantikan": "beauty",
    "fashion": "fashion",
    "otomotif": "vehicle",
    "rumah": "real estate",
    "properti": "real estate",
    "pekerjaan": "jobs",
    "lowongan": "jobs",
    "barang": "shopping products",
    "belanja": "shopping",
    "mata": "currency exchange",
    "kurs": "currency exchange",
    "angka": "numbers math",
    "matematika": "math",
    "waktu": "time date",
    "tanggal": "date time",
    "url": "url shortener",
    "link": "url shortener",
    "keamanan": "security",
    "password": "security",
    "vpn": "security",
    "dns": "development",
    "hosting": "development",
    "kod": "programming",
    "kode": "programming",
    "coding": "programming",
    "programming": "programming",
    "api": "development",
    "website": "development",
    "testing": "test data",
    "dokumen": "documents",
    "pdf": "documents",
    "suara": "music audio",
    "audio": "audio music",
    "podcast": "podcast",
    "radio": "radio",
    "tv": "video streaming",
    "sains": "science",
    "sport": "sports",
}


def _normalise_subject(subject: str) -> str:
    """Turn a casual Indonesian subject into English keywords.

    Multi-word input is translated word by word and joined, so "api cuaca kota"
    becomes "weather kota" and still matches on "weather". Unknown words are
    passed through rather than dropped - a miss is better than a wrong hit.
    """
    words = [w for w in re.split(r"\s+", (subject or "").strip().lower()) if w]
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        bare = re.sub(r"[^a-z0-9]", "", word)
        mapped = SUBJECT_SYNONYMS.get(bare, bare)
        # Two Indonesian words often map to the same English term ("resep" and
        # "makanan" both give "food recipe"); keeping both would double the
        # weight of that term in scoring for no reason.
        for token in mapped.split():
            if token and token not in seen:
                seen.add(token)
                out.append(token)
    return " ".join(out)


def build_catalog(settings) -> ApiCatalog:
    root = Path(getattr(settings, "data_path", Path("data"))) / "catalog"
    return ApiCatalog(root / "public-apis.md")


def wants_api_lookup(text: str) -> tuple[bool, str]:
    """Is the user asking where to get an API, and for what?

    Kept as plain string matching so it costs nothing and runs before any model
    call. The two halves are: a word that means "I want an API/key", and the
    subject. Without a subject the bot asks instead of guessing.
    """
    lowered = (text or "").lower()
    if not re.search(r"\b(api|apikey|api\s*key|kunci\s*api)\b", lowered):
        return False, ""
    if not re.search(
        r"\b(cari|carikan|butuh|perlu|ada\s+ga|ada\s+gak|rekomendasi|saran|daftar|list|mana|apa)\b",
        lowered,
    ):
        return False, ""
    # "ambilkan key gemini" is the harvester's job, not the catalog's.
    if re.search(r"\b(ambil|ambilkan|ambilin|kasih|kasihin|beri|berikan|dapatkan|tarik|pungut)\b", lowered):
        return False, ""
    cleaned = re.sub(r"\b(api\s*key|apikey|api|kunci|gratis|free|untuk|buat|cari|carikan|butuh|perlu|rekomendasi|saran|daftar|list|mana|apa|ada|ga|gak)\b", " ", lowered)
    subject = " ".join(cleaned.split())
    return True, subject
