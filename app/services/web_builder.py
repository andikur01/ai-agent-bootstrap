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
from app.services.design_system import (
    base_stylesheet,
    choose_direction,
    design_brief,
    google_fonts_url,
)

log = get_logger(__name__)

BANNED_PATTERNS = [
    (re.compile(r"lorem\s+ipsum", re.I), "lorem ipsum"),
    (re.compile(r"\bTODO\b"), "TODO placeholder"),
    (re.compile(r"\bFIXME\b"), "FIXME placeholder"),
    (re.compile(r"your[- ]?(api[- ]?key|token)\s*here", re.I), "placeholder secret"),
    (re.compile(r"<img(?![^>]*\balt=)[^>]*>", re.I), "img tanpa alt"),
]

STATIC_SYSTEM = """Kamu frontend engineer yang menulis MARKUP dan KONTEN saja.

PENTING: desainnya SUDAH DISEDIAKAN. Kamu DILARANG menulis file styles.css
sendiri dan DILARANG menulis <style> di dalam HTML. File styles.css sudah ada
dan sudah berisi design system lengkap. Tugasmu mengisi struktur HTML memakai
class yang sudah disediakan.

Kamu WAJIB memakai class berikut (sudah ada di styles.css):
- .wrap          : pembungkus lebar konten
- .site-header   : header lengket, isi .logo + .logo-mark + .nav + .theme-toggle
- .logo, .logo-mark, .nav
- .btn, .btn-primary, .btn-ghost
- .theme-toggle  : tombol dark mode, WAJIB ada, id="theme-toggle"
- .hero, .hero-grid, .eyebrow, .hero-actions
- .card          : kartu konten
- .grid, .grid-2, .grid-3
- .section-head
- .thumb         : pengganti gambar, isi teks singkat di dalamnya
- .price, .badge
- .site-footer   : isi dengan grid berisi .logo + 2 kolom link
- .reveal        : tambahkan ke section/kartu agar muncul saat di-scroll
- .skip-link, .sr-only

Balas JSON:
{
  "title": "<judul situs, spesifik>",
  "description": "<meta description 140-160 karakter>",
  "files": [
    {"path": "index.html", "content": "<HTML lengkap>"},
    {"path": "script.js", "content": "<JS kecil>"}
  ],
  "notes": "<catatan singkat>"
}

JANGAN sertakan styles.css di daftar files. File itu ditulis otomatis.

WAJIB di HTML:
- <!DOCTYPE html>, <html lang="id" data-theme="light">, meta charset, viewport
- <title> spesifik, meta description, Open Graph (og:title, og:description, og:type)
- <link rel="preconnect" href="https://fonts.googleapis.com"> dan stylesheet font
- <link rel="stylesheet" href="styles.css">
- <a class="skip-link" href="#main">Lewati ke konten</a> tepat setelah <body>
- Struktur: header -> main#main (beberapa section) -> footer
- Minimal 5 section bermakna: hero, keunggulan/fitur, produk/layanan, testimoni,
  kontak atau CTA. Sesuaikan dengan permintaan user.
- KONTEN ASLI dan spesifik. DILARANG lorem ipsum. DILARANG "Nama Anda",
  "Deskripsi di sini", "Produk 1", "Lorem". Tulis nama merek yang masuk akal,
  harga nyata, deskripsi yang menjelaskan manfaat.
- Setiap gambar diganti <div class="thumb"> dengan teks di dalamnya.
- aria-label pada tombol ikon, alt pada semua <img> (kalau ada).

WAJIB di script.js:
- Toggle dark mode: baca localStorage, set data-theme di <html>, simpan pilihan,
  sinkron dengan prefers-color-scheme. Tombol harus benar-benar berubah ikonnya.
- IntersectionObserver untuk menambah .is-visible ke semua .reveal.
- Kode defensif: pakai optional chaining, jangan error kalau elemen tidak ada.

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
        direction = choose_direction(request)
        system = APP_TEMPLATES[kind]
        if kind == "static":
            system = f"{system}\n\n{design_brief(direction)}"
        result = await self.gateway.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Permintaan user: {request}"},
            ],
            tier="big",
            temperature=0.5,
            max_tokens=16000,
            json_mode=True,
        )
        data = _parse_json(result.text)
        if not data or not isinstance(data.get("files"), list):
            raise ToolError("Model tidak mengembalikan struktur file yang valid")
        if kind == "static":
            data = self._install_design(data, direction, request)
        return data

    def _install_design(self, spec: dict, direction, request: str) -> dict:
        """Write the design system's CSS into the spec, replacing any the model wrote.

        The model is told not to write styles.css, but a model that ignores that
        must not be able to ship its own palette. Whatever it produced is
        dropped and the hand-written sheet takes its place.
        """
        files = [
            entry
            for entry in spec.get("files", [])
            if str(entry.get("path") or "").strip().lower() not in {"styles.css", "style.css"}
        ]
        files.append({"path": "styles.css", "content": base_stylesheet(direction)})
        spec["files"] = files

        html_entry = next(
            (e for e in files if str(e.get("path") or "").endswith("index.html")), None
        )
        if html_entry is not None:
            html_entry["content"] = _wire_head(str(html_entry.get("content") or ""), direction)
            # Backfill any class the markup uses but the sheet does not define.
            # The prompt lists the allowed classes, but a model will still reach
            # for one it knows from elsewhere; that used to ship as a page with
            # half its layout missing. Now the gap is closed in code, not hoped
            # away, and the build records what it patched.
            sheet, patched = _backfill_missing_classes(
                str(html_entry["content"]), base_stylesheet(direction)
            )
            if patched:
                log.info(
                    "backfilled %d missing CSS classes: %s",
                    len(patched), ", ".join(patched[:10]),
                )
            css_entry = next(e for e in files if e.get("path") == "styles.css")
            css_entry["content"] = sheet
        spec["design"] = direction.key
        return spec

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
            target.write_text(_decode_double_escaped(str(content)), encoding="utf-8")
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


def _wire_head(html: str, direction) -> str:
    """Guarantee the font link, the stylesheet link and data-theme are present.

    A model may omit any of these, and each omission is a visibly broken site:
    no font means system-ui fallback, no stylesheet means unstyled black text on
    white - exactly the failure the user reported.
    """
    if not html.strip():
        return html
    fonts = google_fonts_url(direction)

    # Ensure <html> carries lang and data-theme.
    if re.search(r"<html\b[^>]*>", html, re.I):
        def fix_html(match: str) -> str:
            tag = match.group(0)
            if "lang=" not in tag.lower():
                tag = tag[:-1] + ' lang="id">'
            if "data-theme" not in tag.lower():
                tag = tag[:-1] + ' data-theme="light">'
            return tag

        html = re.sub(r"<html\b[^>]*>", fix_html, html, count=1, flags=re.I)
    else:
        html = '<html lang="id" data-theme="light">' + html

    # Ensure the font stylesheet is linked, right before </head>.
    font_links = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        f'<link rel="stylesheet" href="{fonts}">'
    )
    if "fonts.googleapis.com" not in html:
        if "</head>" in html.lower():
            html = re.sub(r"</head>", font_links + "\n</head>", html, count=1, flags=re.I)
        else:
            html = font_links + html

    # Ensure our stylesheet is linked last so it wins over inline styles.
    html = re.sub(
        r'<link[^>]+href=["\'](?:\./)?(?:styles|style)\.css["\'][^>]*>',
        "",
        html,
        flags=re.I,
    )
    stylesheet = '<link rel="stylesheet" href="styles.css">'
    if "</head>" in html.lower():
        html = re.sub(r"</head>", stylesheet + "\n</head>", html, count=1, flags=re.I)
    else:
        html = stylesheet + html
    return html


def _decode_double_escaped(content: str) -> str:
    """Undo a second round of escaping that some models emit inside JSON strings.

    The model is asked for a JSON object whose values are file contents. Some
    models return valid JSON in which the value itself is an escaped string:
    a newline arrives as the two characters ``\\n`` instead of a real newline.
    The JSON parses fine, so nothing complains, and the written file is one
    long line of ``\\n`` - which is exactly the "Invalid or unexpected token"
    JavaScript error the QA browser reported. Decode only when the whole file
    is clearly escaped, so real backslashes in code are left alone.
    """
    if "\\n" not in content or "\n" in content:
        return content
    try:
        # latin-1 with backslashreplace keeps non-ASCII characters (emoji, accents)
        # intact: they become \uXXXX escapes which unicode_escape then restores.
        # Encoding as utf-8 instead would mojibake every emoji in the page.
        decoded = content.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        return content
    if decoded.count("\n") == content.count("\\n"):
        return decoded
    return content


def _backfill_missing_classes(html: str, sheet: str) -> tuple[str, list[str]]:
    """Define every class the markup uses but the stylesheet does not.

    The prompt names the classes the model may use, and the stylesheet defines
    them. In practice a model also reaches for a utility it knows from elsewhere
    (``space-x-4``, ``items-center``), and an undefined class is not an error -
    the layout silently loses a rule. That is how a site shipped looking plain.

    Rather than trusting the model, measure: read the classes out of the HTML,
    diff them against the sheet, and append a rule for each gap. Unknown names
    get a safe default keyed off the name's prefix. Returns the sheet to use and
    the names that were patched, for the log.
    """
    used: set[str] = set()
    for match in re.findall(r'class="([^"]*)"', html):
        used.update(part for part in match.split() if part)

    # A class counts as defined only if the sheet has a selector for it.
    missing = [
        name for name in sorted(used)
        if not re.search(rf"\.{re.escape(name)}(?![\w-])", sheet)
    ]
    if not missing:
        return sheet, []

    rules = [_default_rule_for(name) for name in missing]
    block = "\n/* --- auto: classes the markup used but the sheet lacked --- */\n" + "\n".join(rules)
    return sheet + block + "\n", missing


def _default_rule_for(name: str) -> str:
    """A safe rule for an unknown class, chosen from its name.

    A utility the model invented should do *something* reasonable rather than
    nothing. The mapping is deliberately small and conservative: it covers the
    utility families a page actually needs and leaves anything unrecognised as a
    no-op, because inventing a visual style for an unknown name is worse than
    leaving it alone.
    """
    n = name.lower()
    if n.startswith("gap-"):
        value = _scale_value(n)
        return f".{name} {{ gap: {value}; }}"
    if n.startswith("space-x-"):
        return f".{name} > * + * {{ margin-left: {_scale_value(n)}; }}"
    if n.startswith("space-y-"):
        return f".{name} > * + * {{ margin-top: {_scale_value(n)}; }}"
    if n.startswith("p-"):
        return f".{name} {{ padding: {_scale_value(n)}; }}"
    if n.startswith("px-"):
        return f".{name} {{ padding-inline: {_scale_value(n)}; }}"
    if n.startswith("py-"):
        return f".{name} {{ padding-block: {_scale_value(n)}; }}"
    if n.startswith("m-"):
        return f".{name} {{ margin: {_scale_value(n)}; }}"
    if n.startswith("mt-"):
        return f".{name} {{ margin-top: {_scale_value(n)}; }}"
    if n.startswith("mb-"):
        return f".{name} {{ margin-bottom: {_scale_value(n)}; }}"
    if n.startswith("text-") and n.endswith(("xl", "lg", "sm", "xs", "base")):
        return f".{name} {{ font-size: {_font_scale(n)}; }}"
    if n.startswith("rounded"):
        return f".{name} {{ border-radius: var(--radius); }}"
    if n.startswith("shadow"):
        return f".{name} {{ box-shadow: var(--shadow); }}"
    if n.startswith("w-"):
        return f".{name} {{ width: {_scale_value(n)}; }}"
    if n.startswith("max-w-"):
        return f".{name} {{ max-width: {_scale_value(n)}; }}"
    if n.startswith("font-bold"):
        return f".{name} {{ font-weight: 700; }}"
    if n.startswith("font-semibold"):
        return f".{name} {{ font-weight: 600; }}"
    if n.startswith("text-center"):
        return f".{name} {{ text-align: center; }}"
    return f".{name} {{ /* auto: unknown utility, intentionally a no-op */ }}"


def _scale_value(name: str) -> str:
    """Map a Tailwind-ish suffix to a real length. ``gap-6`` -> ``1.5rem``."""
    digits = re.findall(r"(\d+)", name)
    if not digits:
        return "1rem"
    step = int(digits[-1])
    # Tailwind's 4px scale, capped so a typo like gap-96 cannot blow up a layout.
    step = max(0, min(step, 16))
    return f"{step * 0.25:g}rem"


def _font_scale(name: str) -> str:
    table = {"xs": ".78rem", "sm": ".875rem", "base": "1rem", "lg": "1.15rem",
             "xl": "1.3rem", "2xl": "1.6rem", "3xl": "2rem", "4xl": "2.6rem"}
    for key, value in table.items():
        if name.endswith(key):
            return value
    return "1rem"


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
