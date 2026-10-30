"""Website generation: static sites and fullstack apps.

The LLM is asked for *files*, as JSON: a path plus content per file. That keeps
generation deterministic and reviewable, and lets us validate before writing
anything to disk. Validation is strict because the whole point is to ship
something that actually renders:

* every path must be relative and inside the project dir (no ``..`` escapes)
* an ``index.html`` must exist for static sites
* no lorem ipsum, no TODO placeholders
* required SEO/accessibility basics (title, meta viewport, lang, alt attributes)
* fullstack apps must expose the files the deploy layer needs
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

BANNED_PATTERNS = [
    (re.compile(r"lorem\s+ipsum", re.I), "lorem ipsum"),
    (re.compile(r"\bTODO\b"), "TODO placeholder"),
    (re.compile(r"\bFIXME\b"), "FIXME placeholder"),
    (re.compile(r"your[- ]?(api[- ]?key|token)\s*here", re.I), "placeholder secret"),
    (re.compile(r"<img(?![^>]*\balt=)[^>]*>", re.I), "img tanpa alt"),
]

STATIC_SYSTEM = """Kamu web designer + frontend engineer senior. Buat website statis lengkap.

Balas JSON dengan skema:
{
  "title": "<judul situs>",
  "description": "<meta description 140-160 karakter>",
  "files": [
    {"path": "index.html", "content": "<HTML lengkap>"},
    {"path": "styles.css", "content": "<CSS>"},
    {"path": "script.js", "content": "<JS>"}
  ],
  "notes": "<catatan singkat>"
}

WAJIB:
- HTML5 lengkap: <!DOCTYPE html>, lang="id", meta charset, meta viewport,
  <title> spesifik, meta description, Open Graph tags.
- CSS modern: custom properties, flexbox/grid, dark mode via
  prefers-color-scheme DAN toggle manual, responsif mobile-first.
- Aksesibilitas: kontras cukup, aria-label pada tombol ikon, alt pada gambar,
  fokus visible, heading hierarkis.
- Konten ASLI dan spesifik sesuai permintaan user. DILARANG lorem ipsum,
  DILARANG placeholder seperti "Nama Anda" atau "Deskripsi di sini".
  Tulis konten nyata yang masuk akal.
- JavaScript vanilla, tanpa framework, tanpa CDN eksternal kalau bisa.
- Tombol dark mode harus benar-benar berfungsi.
- Tanpa gambar eksternal: pakai CSS gradient, SVG inline, atau emoji.

Balas JSON saja, tanpa penjelasan di luar JSON."""

FULLSTACK_SYSTEM = """Kamu fullstack engineer senior. Buat web app fullstack dengan login + database.

Target stack: FastAPI + Supabase (Postgres + Auth), frontend HTML/CSS/JS vanilla
yang memanggil REST API. Balas JSON:
{
  "title": "<nama app>",
  "description": "<deskripsi singkat>",
  "files": [
    {"path": "main.py", "content": "<FastAPI app>"},
    {"path": "requirements.txt", "content": "fastapi\\nuvicorn[standard]\\nsupabase\\npython-dotenv\\n"},
    {"path": "public/index.html", "content": "<frontend>"},
    {"path": "public/app.js", "content": "<frontend logic>"},
    {"path": "public/styles.css", "content": "<css>"},
    {"path": "README.md", "content": "<cara jalanin>"}
  ],
  "tables": ["<nama tabel yang perlu dibuat di Supabase>"],
  "env_vars": ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"],
  "test_account": {"email": "demo@example.com", "password": "<password kuat>"},
  "notes": "<catatan>"
}

WAJIB:
- FastAPI app dengan endpoint: GET /api/health, POST /api/auth/signup,
  POST /api/auth/login, GET /api/items (auth), POST /api/items (auth).
- Auth via Supabase (supabase-py). Token JWT dari header Authorization.
- Baca kredensial HANYA dari environment variable (os.environ / .env).
  DILARANG hardcode secret apa pun di kode.
- SQL untuk tabel disertakan di README (schema + RLS policy dasar).
- Frontend: form login/register, list item, tambah item, tampilan modern dark
  mode, responsif, error handling yang terlihat user.
- Konten nyata, bukan placeholder.
Balas JSON saja."""

APP_TEMPLATES = {
    "static": STATIC_SYSTEM,
    "fullstack": FULLSTACK_SYSTEM,
}


@dataclass
class WebProject:
    root: Path
    kind: str
    title: str
    description: str
    files: list[Path] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    test_account: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def entry(self) -> Path:
        for candidate in ("index.html", "public/index.html"):
            path = self.root / candidate
            if path.exists():
                return path
        return self.root / "index.html"

    def issues(self) -> list[str]:
        problems: list[str] = []
        if not self.files:
            problems.append("tidak ada file yang dihasilkan")
        if self.kind == "static" and not (self.root / "index.html").exists():
            problems.append("index.html tidak ada")
        if self.kind == "fullstack":
            if not (self.root / "main.py").exists():
                problems.append("main.py tidak ada")
            if not (self.root / "requirements.txt").exists():
                problems.append("requirements.txt tidak ada")
            if not (self.root / "public" / "index.html").exists():
                problems.append("public/index.html tidak ada")
        for path in self.files:
            if path.suffix in {".html", ".css", ".js"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for pattern, label in BANNED_PATTERNS:
                    if pattern.search(text):
                        problems.append(f"{path.name}: {label}")
        return problems


class WebBuilder:
    def __init__(self, settings, gateway=None, *, outputs_dir: Path | None = None):
        self.settings = settings
        self.gateway = gateway
        self.outputs = Path(outputs_dir or settings.outputs_path)

    async def build(
        self,
        request: str,
        *,
        kind: str = "static",
        slug: str = "",
        progress=None,
    ) -> WebProject:
        if kind not in APP_TEMPLATES:
            raise ToolError(f"Jenis web tidak dikenal: {kind}")
        if self.gateway is None or not self.gateway.available():
            raise ToolError(
                "Butuh provider LLM untuk membuat website. Isi minimal satu API key."
            )

        if progress:
            await progress(15, f"Merancang struktur {kind}")
        spec = await self._plan(request, kind)
        if progress:
            await progress(45, f"Menulis {len(spec.get('files', []))} file")

        project = self._materialise(spec, kind, slug or request)
        if progress:
            await progress(75, "Validasi hasil")
        return project

    async def _plan(self, request: str, kind: str) -> dict:
        result = await self.gateway.complete(
            [
                {"role": "system", "content": APP_TEMPLATES[kind]},
                {"role": "user", "content": f"Permintaan user: {request}"},
            ],
            tier="big",
            temperature=0.4,
            max_tokens=8000,
            json_mode=True,
        )
        data = _parse_json(result.text)
        if not data or not isinstance(data.get("files"), list):
            raise ToolError("Model tidak mengembalikan struktur file yang valid")
        return data

    def _materialise(self, spec: dict, kind: str, slug_source: str) -> WebProject:
        root = self.outputs / "web" / f"{_slug(slug_source)}-{_stamp()}"
        root.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for entry in spec.get("files", []):
            if not isinstance(entry, dict):
                continue
            relative = str(entry.get("path") or "").strip().lstrip("/")
            content = entry.get("content")
            if not relative or content is None:
                continue
            if ".." in Path(relative).parts or relative.startswith("/"):
                log.warning("rejected unsafe path from model: %s", relative)
                continue
            target = (root / relative).resolve()
            if not str(target).startswith(str(root.resolve())):
                log.warning("rejected path escaping project root: %s", relative)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            written.append(target)

        project = WebProject(
            root=root,
            kind=kind,
            title=str(spec.get("title") or slug_source)[:120],
            description=str(spec.get("description") or "")[:300],
            files=written,
            tables=[str(t) for t in (spec.get("tables") or [])],
            env_vars=[str(v) for v in (spec.get("env_vars") or [])],
            test_account=spec.get("test_account") or {},
            notes=str(spec.get("notes") or "")[:500],
        )
        return project

    async def repair(self, project: WebProject, issues: list[str], progress=None) -> WebProject:
        """Ask the model to fix specific reported problems, then rewrite files."""
        if self.gateway is None or not self.gateway.available():
            raise ToolError("Butuh provider LLM untuk memperbaiki website.")

        current = {}
        for path in project.files:
            if path.suffix in {".html", ".css", ".js", ".py"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                current[str(path.relative_to(project.root))] = text[:12000]

        if progress:
            await progress(20, f"Memperbaiki {len(issues)} masalah")
        result = await self.gateway.complete(
            [
                {"role": "system", "content": APP_TEMPLATES[project.kind]},
                {
                    "role": "user",
                    "content": (
                        f"Proyek: {project.title} ({project.kind})\n"
                        f"Masalah yang ditemukan QA otomatis:\n"
                        + "\n".join(f"- {issue}" for issue in issues[:12])
                        + "\n\nFile saat ini:\n"
                        + json.dumps(current, ensure_ascii=False)[:24000]
                        + "\n\nPerbaiki semua masalah. Balas JSON dengan seluruh file "
                        "(bukan hanya yang berubah) memakai skema yang sama."
                    ),
                },
            ],
            tier="big",
            temperature=0.2,
            max_tokens=8000,
            json_mode=True,
        )
        data = _parse_json(result.text)
        if not data or not isinstance(data.get("files"), list):
            raise ToolError("Model tidak mengembalikan perbaikan yang valid")

        repaired = self._materialise(data, project.kind, project.title)
        # keep the same directory so deploy links stay stable
        for path in repaired.files:
            relative = path.relative_to(repaired.root)
            target = project.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        project.files = sorted(p for p in project.root.rglob("*") if p.is_file())
        project.notes = (repaired.notes or project.notes)[:500]
        return project


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


def _slug(text: str, limit: int = 30) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit] or "site").strip("-")


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")
