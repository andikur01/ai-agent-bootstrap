"""Design directions: the antidote to black-and-white slop.

Why generated sites looked cheap: the model was told "make it modern" and given
no palette, no type scale and no reference, so it produced system-ui on white
with a #007bff button. Every site looked identical and none looked designed.

The fix is not a bigger prompt. It is removing the choice: hand the model a
finished design system - real fonts, a real palette, real components - and ask
it to fill in content, not to invent a look. A designer would not start from
nothing either.

Each direction is a complete, opinionated system: type pairing, colour tokens,
radii, shadows, and a signature element that makes the site recognisable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DesignDirection:
    key: str
    label: str
    mood: str
    fonts: str
    palette: dict[str, str]
    radius: str
    shadow: str
    signature: str
    font_weights: str = "400;500;600;700;800"
    extra: list[str] = field(default_factory=list)

    def css_vars(self, dark: bool = False) -> str:
        tokens = self.palette
        lines = [f"  --{name}: {value};" for name, value in tokens.items()]
        return "\n".join(lines)


# Curated, opinionated systems. Deliberately different from each other: the
# point is that a coffee shop and a SaaS dashboard must not look the same.
DIRECTIONS: dict[str, DesignDirection] = {
    "aurora": DesignDirection(
        key="aurora",
        label="Aurora",
        mood="SaaS modern, bersih, terpercaya",
        fonts="Inter (UI) + Inter Display untuk heading",
        palette={
            "bg": "#f7f8fb",
            "surface": "#ffffff",
            "surface-2": "#eef1f8",
            "text": "#0d1117",
            "text-muted": "#5b6478",
            "border": "#e2e6ef",
            "brand": "#4f46e5",
            "brand-strong": "#4338ca",
            "brand-soft": "#eef0ff",
            "accent": "#06b6d4",
            "success": "#10b981",
            "warning": "#f59e0b",
            "danger": "#ef4444",
        },
        radius="14px",
        shadow="0 1px 2px rgba(13,17,23,.06), 0 12px 32px -12px rgba(13,17,23,.16)",
        signature="Hero dua kolom dengan kartu produk mengambang dan gradien lembut di belakangnya",
        extra=["Gradien lembut brand->accent hanya di elemen hero, jangan di mana-mana"],
    ),
    "atelier": DesignDirection(
        key="atelier",
        label="Atelier",
        mood="Editorial, premium, banyak ruang kosong",
        fonts="Fraunces (display serif) + Inter (teks)",
        palette={
            "bg": "#faf7f2",
            "surface": "#ffffff",
            "surface-2": "#f2ece2",
            "text": "#1c1917",
            "text-muted": "#6b6459",
            "border": "#e6ded1",
            "brand": "#9a3412",
            "brand-strong": "#7c2d12",
            "brand-soft": "#fdf0e7",
            "accent": "#0f766e",
            "success": "#15803d",
            "warning": "#b45309",
            "danger": "#b91c1c",
        },
        radius="4px",
        shadow="0 1px 2px rgba(28,25,23,.05), 0 20px 40px -24px rgba(28,25,23,.25)",
        signature="Tipografi display besar dengan garis tipis dan nomor bagian seperti majalah",
        extra=["Sudut hampir siku (radius kecil) supaya terasa cetak, bukan aplikasi"],
    ),
    "midnight": DesignDirection(
        key="midnight",
        label="Midnight",
        mood="Premium gelap, teknologi, fokus",
        fonts="Space Grotesk (heading) + Inter (teks)",
        palette={
            "bg": "#08090d",
            "surface": "#101218",
            "surface-2": "#171a23",
            "text": "#f2f4f8",
            "text-muted": "#98a0b3",
            "border": "#232734",
            "brand": "#7c5cff",
            "brand-strong": "#6d4aff",
            "brand-soft": "#191735",
            "accent": "#22d3ee",
            "success": "#34d399",
            "warning": "#fbbf24",
            "danger": "#f87171",
        },
        radius="12px",
        shadow="0 1px 0 rgba(255,255,255,.04) inset, 0 24px 48px -24px rgba(0,0,0,.8)",
        signature="Permukaan gelap dengan garis tepi bercahaya halus dan aksen neon tipis",
        extra=["Mode terang tetap disediakan, tapi default gelap"],
    ),
    "terracotta": DesignDirection(
        key="terracotta",
        label="Terracotta",
        mood="Hangat, ramah, cocok untuk toko & kuliner",
        fonts="Plus Jakarta Sans untuk semua teks",
        palette={
            "bg": "#fffaf5",
            "surface": "#ffffff",
            "surface-2": "#fdf1e7",
            "text": "#2b1a12",
            "text-muted": "#7a6152",
            "border": "#f0dfd0",
            "brand": "#c2410c",
            "brand-strong": "#9a3412",
            "brand-soft": "#ffedd5",
            "accent": "#0d9488",
            "success": "#16a34a",
            "warning": "#d97706",
            "danger": "#dc2626",
        },
        radius="20px",
        shadow="0 2px 4px rgba(43,26,18,.06), 0 18px 36px -18px rgba(194,65,12,.28)",
        signature="Kartu produk besar dengan sudut membulat dan harga yang menonjol",
        extra=["Foto produk pakai placeholder gradien hangat, bukan kotak abu"],
    ),
    "neo": DesignDirection(
        key="neo",
        label="Neo",
        mood="Berani, tegas, tipografi besar",
        fonts="Archivo (heading tebal) + Inter (teks)",
        palette={
            "bg": "#ffffff",
            "surface": "#ffffff",
            "surface-2": "#f4f4f5",
            "text": "#09090b",
            "text-muted": "#52525b",
            "border": "#09090b",
            "brand": "#18181b",
            "brand-strong": "#000000",
            "brand-soft": "#f4f4f5",
            "accent": "#e11d48",
            "success": "#15803d",
            "warning": "#ca8a04",
            "danger": "#dc2626",
        },
        radius="0px",
        shadow="6px 6px 0 #09090b",
        signature="Blok warna tebal, garis hitam tegas, dan satu warna aksen yang berani",
        font_weights="400;600;700;800;900",
        extra=["Sudut siku total dan garis tebal: ini pilihan gaya, bukan kelalaian"],
    ),
}

# Keyword to direction. Order matters: the most specific keyword sets come
# first, because a generic word like "aplikasi" would otherwise capture a crypto
# product before "kripto" ever gets a chance.
_ROUTING: list[tuple[tuple[str, ...], str]] = [
    (("kripto", "crypto", "trading", "game", "gaming", "esport", "clan", "musik", "devtool", "api"), "midnight"),
    (("portofolio", "portfolio", "fotografer", "studio", "majalah", "editorial", "hotel", "villa", "wedding", "arsitek", "galeri", "resort", "spa", "seni"), "atelier"),
    (("kopi", "cafe", "kafe", "restoran", "restaurant", "makanan", "kuliner", "toko", "butik", "fashion", "kue", "bakery", "katering", "jualan", "umkm", "warung"), "terracotta"),
    (("branding", "agen", "event", "komunitas", "sekolah", "kursus", "olahraga", "gym", "berita", "media"), "neo"),
    (("laptop", "komputer", "gadget", "elektronik", "startup", "saas", "dashboard", "aplikasi", "jasa", "konsultan", "software"), "aurora"),
]


def choose_direction(request: str) -> DesignDirection:
    """Pick a direction from the request, defaulting to Aurora.

    Matching is on word boundaries. A substring match sent "kedai kopi" to the
    tech palette because "kedai" contains "ai" - the wrong direction, silently,
    which is how a coffee shop ends up looking like a SaaS dashboard.
    """
    lowered = request.lower()
    for keywords, key in _ROUTING:
        for word in keywords:
            pattern = r"\b" + re.escape(word) + r"\b"
            if re.search(pattern, lowered):
                return DIRECTIONS[key]
    return DIRECTIONS["aurora"]


def google_fonts_url(direction: DesignDirection) -> str:
    """A single Google Fonts request for the direction's families."""
    families: list[str] = []
    if direction.key == "aurora":
        families.append("Inter:wght@400;500;600;700;800")
    elif direction.key == "atelier":
        families.append("Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700")
        families.append("Inter:wght@400;500;600")
    elif direction.key == "midnight":
        families.append("Space+Grotesk:wght@500;600;700")
        families.append("Inter:wght@400;500;600")
    elif direction.key == "terracotta":
        families.append("Plus+Jakarta+Sans:wght@400;500;600;700;800")
    else:
        families.append("Archivo:wght@600;700;800;900")
        families.append("Inter:wght@400;500;600")
    joined = "&".join(f"family={f}" for f in families)
    return f"https://fonts.googleapis.com/css2?{joined}&display=swap"


def font_stacks(direction: DesignDirection) -> tuple[str, str]:
    """Return (heading_stack, body_stack) as CSS font-family values."""
    stacks = {
        "aurora": ("'Inter', system-ui, sans-serif", "'Inter', system-ui, sans-serif"),
        "atelier": ("'Fraunces', Georgia, serif", "'Inter', system-ui, sans-serif"),
        "midnight": ("'Space Grotesk', system-ui, sans-serif", "'Inter', system-ui, sans-serif"),
        "terracotta": ("'Plus Jakarta Sans', system-ui, sans-serif", "'Plus Jakarta Sans', system-ui, sans-serif"),
        "neo": ("'Archivo', system-ui, sans-serif", "'Inter', system-ui, sans-serif"),
    }
    return stacks.get(direction.key, ("system-ui, sans-serif", "system-ui, sans-serif"))


def dark_tokens(direction: DesignDirection) -> dict[str, str]:
    """A dark variant that keeps the direction's character.

    Not an inversion: warm stays warm, neon stays neon, just darker surfaces.
    """
    base = dict(direction.palette)
    variants = {
        "aurora": {
            "bg": "#0b0d12", "surface": "#12151d", "surface-2": "#181c27",
            "text": "#eef1f8", "text-muted": "#98a1b8", "border": "#242938",
            "brand-soft": "#1a1b3a",
        },
        "atelier": {
            "bg": "#141210", "surface": "#1c1917", "surface-2": "#262220",
            "text": "#f5f0e8", "text-muted": "#a89e90", "border": "#332e29",
            "brand-soft": "#2a1a12", "brand": "#fb923c",
        },
        "midnight": dict(base),
        "terracotta": {
            "bg": "#171009", "surface": "#1f1610", "surface-2": "#2a1e15",
            "text": "#fdf2e9", "text-muted": "#b39a86", "border": "#3a2a1d",
            "brand-soft": "#33200f", "brand": "#fb923c",
        },
        "neo": {
            "bg": "#0a0a0a", "surface": "#141414", "surface-2": "#1f1f1f",
            "text": "#fafafa", "text-muted": "#a1a1aa", "border": "#fafafa",
            "brand": "#fafafa", "brand-strong": "#ffffff", "brand-soft": "#1f1f1f",
        },
    }
    base.update(variants.get(direction.key, {}))
    return base


def base_stylesheet(direction: DesignDirection) -> str:
    """A complete, hand-written stylesheet for the direction.

    The model writes content and markup; this writes the design. That split is
    what stops the black-and-white slop: the visual layer is not left to a
    language model that has no taste and no reference.
    """
    tokens = "\n".join(f"  --{k}: {v};" for k, v in direction.palette.items())
    dark = "\n".join(f"  --{k}: {v};" for k, v in dark_tokens(direction).items())
    heading, body = font_stacks(direction)
    radius = direction.radius
    shadow = direction.shadow
    # Neo has square corners and a hard shadow by design; a "nice" radius would
    # erase the whole point of the direction.
    radius_sm = "0" if radius == "0px" else f"calc({radius} * 0.5)"
    radius_lg = "0" if radius == "0px" else f"calc({radius} * 1.5)"

    return f"""/* Design system: {direction.label}. Hand-written, do not replace. */
:root {{
{tokens}
  --radius: {radius};
  --radius-sm: {radius_sm};
  --radius-lg: {radius_lg};
  --shadow: {shadow};
  --shadow-lg: 0 24px 60px -24px rgba(0,0,0,.28);
  --font-heading: {heading};
  --font-body: {body};
  --wrap: 1200px;
  --space: clamp(3rem, 8vw, 7.5rem);
  --ease: cubic-bezier(.22,.61,.36,1);
  color-scheme: light;
}}

@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
{dark}
    color-scheme: dark;
  }}
}}

:root[data-theme="dark"] {{
{dark}
  color-scheme: dark;
}}

*, *::before, *::after {{ box-sizing: border-box; }}

html {{ scroll-behavior: smooth; -webkit-text-size-adjust: 100%; }}

body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  font-size: clamp(1rem, .96rem + .2vw, 1.0625rem);
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}}

h1, h2, h3, h4 {{
  font-family: var(--font-heading);
  line-height: 1.1;
  letter-spacing: -0.02em;
  margin: 0 0 .6em;
  text-wrap: balance;
}}

h1 {{ font-size: clamp(2.4rem, 5vw, 4rem); font-weight: 800; }}
h2 {{ font-size: clamp(1.8rem, 3.2vw, 2.6rem); font-weight: 700; }}
h3 {{ font-size: clamp(1.2rem, 1.8vw, 1.5rem); font-weight: 600; }}
p {{ margin: 0 0 1em; color: var(--text-muted); max-width: 68ch; }}

a {{ color: var(--brand); text-decoration: none; transition: color .2s var(--ease); }}
a:hover {{ color: var(--brand-strong); }}

img, svg {{ max-width: 100%; height: auto; display: block; }}

:focus-visible {{
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: var(--radius-sm);
}}

.wrap {{ width: min(100% - 2.5rem, var(--wrap)); margin-inline: auto; }}

/* ---------- header ---------- */
.site-header {{
  position: sticky; top: 0; z-index: 50;
  backdrop-filter: blur(14px);
  background: color-mix(in srgb, var(--bg) 82%, transparent);
  border-bottom: 1px solid var(--border);
}}
.site-header .wrap {{
  display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; min-height: 68px;
}}
.logo {{
  font-family: var(--font-heading); font-weight: 800;
  font-size: 1.25rem; letter-spacing: -.02em; margin: 0;
  color: var(--text); display: flex; align-items: center; gap: .6rem;
}}
.logo-mark {{
  width: 34px; height: 34px; border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--brand), var(--accent));
  display: grid; place-items: center; color: #fff; font-weight: 800;
  font-size: .95rem; flex: none;
}}
.nav {{ display: flex; align-items: center; gap: 1.75rem; }}
.nav a {{ color: var(--text-muted); font-weight: 500; font-size: .95rem; }}
.nav a:hover {{ color: var(--text); }}

/* ---------- buttons ---------- */
.btn {{
  display: inline-flex; align-items: center; justify-content: center; gap: .5rem;
  padding: .875rem 1.75rem; border-radius: var(--radius);
  font-family: var(--font-body); font-weight: 600; font-size: .95rem;
  border: 1px solid transparent; cursor: pointer; text-align: center;
  transition: transform .2s var(--ease), background .2s var(--ease),
    box-shadow .2s var(--ease), border-color .2s var(--ease);
}}
.btn-primary {{ background: var(--brand); color: #fff; box-shadow: var(--shadow); }}
.btn-primary:hover {{ background: var(--brand-strong); color: #fff; transform: translateY(-2px); }}
.btn-ghost {{ background: transparent; color: var(--text); border-color: var(--border); }}
.btn-ghost:hover {{ border-color: var(--brand); color: var(--brand); }}

.theme-toggle {{
  width: 42px; height: 42px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text); cursor: pointer; display: grid; place-items: center;
  font-size: 1.1rem; transition: border-color .2s var(--ease);
}}
.theme-toggle:hover {{ border-color: var(--brand); }}

/* ---------- hero ---------- */
.hero {{ padding: var(--space) 0; position: relative; overflow: hidden; }}
.hero::before {{
  content: ""; position: absolute; inset: -30% -10% auto -10%; height: 70%;
  background: radial-gradient(60% 60% at 50% 0%, color-mix(in srgb, var(--brand) 18%, transparent), transparent 70%);
  pointer-events: none;
}}
.hero .wrap {{ position: relative; }}
.hero-grid {{
  display: grid; gap: clamp(2rem, 5vw, 4rem); align-items: center;
  grid-template-columns: 1fr;
}}
@media (min-width: 900px) {{ .hero-grid {{ grid-template-columns: 1.05fr .95fr; }} }}
.eyebrow {{
  display: inline-flex; align-items: center; gap: .5rem;
  padding: .4rem .9rem; border-radius: 999px;
  background: var(--brand-soft); color: var(--brand-strong);
  font-size: .82rem; font-weight: 600; letter-spacing: .02em;
  margin-bottom: 1.25rem; border: 1px solid color-mix(in srgb, var(--brand) 22%, transparent);
}}
.hero p {{ font-size: clamp(1.05rem, 1.5vw, 1.25rem); }}
.hero-actions {{ display: flex; flex-wrap: wrap; gap: .875rem; margin-top: 2rem; }}

/* ---------- cards ---------- */
.card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-lg); padding: clamp(1.25rem, 2.5vw, 2rem);
  box-shadow: var(--shadow);
  transition: transform .28s var(--ease), box-shadow .28s var(--ease);
}}
.card:hover {{ transform: translateY(-4px); box-shadow: var(--shadow-lg); }}

.grid {{ display: grid; gap: clamp(1rem, 2.5vw, 1.75rem); grid-template-columns: 1fr; }}
@media (min-width: 720px) {{ .grid-2, .grid-3 {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (min-width: 1000px) {{ .grid-3 {{ grid-template-columns: repeat(3, 1fr); }} }}

section {{ padding-block: clamp(3rem, 7vw, 6rem); }}
.section-head {{ max-width: 62ch; margin-bottom: clamp(2rem, 4vw, 3rem); }}

/* ---------- media placeholder (no external images) ---------- */
.thumb {{
  aspect-ratio: 4 / 3; border-radius: var(--radius);
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--brand) 78%, #fff), color-mix(in srgb, var(--accent) 62%, #fff));
  display: grid; place-items: center; color: #fff; font-weight: 700;
  font-size: 1.05rem; letter-spacing: .02em; text-align: center; padding: 1rem;
}}

.price {{ font-family: var(--font-heading); font-weight: 800; font-size: 1.35rem; color: var(--text); }}
.badge {{
  display: inline-block; padding: .25rem .7rem; border-radius: 999px;
  background: var(--brand-soft); color: var(--brand-strong);
  font-size: .78rem; font-weight: 600;
}}

/* ---------- footer ---------- */
.site-footer {{
  border-top: 1px solid var(--border); background: var(--surface-2);
  padding-block: clamp(2.5rem, 5vw, 4rem); margin-top: clamp(3rem, 7vw, 6rem);
}}
.site-footer .wrap {{ display: grid; gap: 2rem; grid-template-columns: 1fr; }}
@media (min-width: 760px) {{ .site-footer .wrap {{ grid-template-columns: 1.5fr 1fr 1fr; }} }}
.site-footer h4 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); }}
.site-footer ul {{ list-style: none; padding: 0; margin: 0; display: grid; gap: .6rem; }}
.site-footer a {{ color: var(--text-muted); font-size: .95rem; }}
.site-footer a:hover {{ color: var(--brand); }}

/* ---------- reveal on scroll ---------- */
.reveal {{ opacity: 0; transform: translateY(18px); transition: opacity .6s var(--ease), transform .6s var(--ease); }}
.reveal.is-visible {{ opacity: 1; transform: none; }}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ animation-duration: .01ms !important; transition-duration: .01ms !important; }}
  html {{ scroll-behavior: auto; }}
  .reveal {{ opacity: 1; transform: none; }}
}}

.skip-link {{
  position: absolute; left: -9999px; top: 0; z-index: 100;
  background: var(--brand); color: #fff; padding: .75rem 1.25rem; border-radius: 0 0 var(--radius-sm) 0;
}}
.skip-link:focus {{ left: 0; }}

.sr-only {{
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}}

/* ---------- layout utilities ----------
   The model reaches for these names because they are what it was trained on.
   They were missing from the stylesheet, so `class="flex items-center
   justify-between"` on a header did nothing and the page collapsed into a
   single column of unstyled blocks. Every utility the prompt mentions must
   exist here, or the markup and the design drift apart. */
.flex {{ display: flex; }}
.inline-flex {{ display: inline-flex; }}
.grid-2, .grid-3 {{ display: grid; gap: clamp(1rem, 2.5vw, 1.75rem); grid-template-columns: 1fr; }}
.items-center {{ align-items: center; }}
.items-start {{ align-items: flex-start; }}
.justify-between {{ justify-content: space-between; }}
.justify-center {{ justify-content: center; }}
.flex-wrap {{ flex-wrap: wrap; }}
.gap-2 {{ gap: .5rem; }}
.gap-4 {{ gap: 1rem; }}
.gap-6 {{ gap: 1.5rem; }}
.space-x-4 > * + * {{ margin-left: 1rem; }}
.space-y-2 > * + * {{ margin-top: .5rem; }}
.space-y-4 > * + * {{ margin-top: 1rem; }}
.list-none {{ list-style: none; padding: 0; margin: 0; }}
.text-center {{ text-align: center; }}
.mx-auto {{ margin-inline: auto; }}
.mt-0 {{ margin-top: 0; }}
.mb-0 {{ margin-bottom: 0; }}
.hero-content {{ display: grid; gap: 1rem; align-content: center; }}

/* Typography the markup refers to. `.h1`/`.lead` are not element selectors, so
   they needed real rules or the hero fell back to plain body text. */
.h1 {{ font-size: clamp(2.4rem, 5vw, 4rem); font-weight: 800; line-height: 1.1;
  letter-spacing: -.02em; margin: 0 0 .4em; }}
.h2 {{ font-size: clamp(1.8rem, 3.2vw, 2.6rem); font-weight: 700; line-height: 1.15;
  letter-spacing: -.02em; margin: 0 0 .5em; }}
.h3 {{ font-size: clamp(1.2rem, 1.8vw, 1.5rem); font-weight: 600; margin: 0 0 .5em; }}
.lead {{ font-size: clamp(1.05rem, 1.6vw, 1.3rem); color: var(--text-muted); max-width: 60ch; }}

@media (min-width: 720px) {{
  .grid-2, .grid-3 {{ grid-template-columns: repeat(2, 1fr); }}
}}
@media (min-width: 1000px) {{
  .grid-3 {{ grid-template-columns: repeat(3, 1fr); }}
}}
"""


def design_brief(direction: DesignDirection) -> str:
    tokens = "\n".join(f"  --{k}: {v};" for k, v in direction.palette.items())
    extra = "".join(f"\n- {line}" for line in direction.extra)
    heading, body = font_stacks(direction)
    return f"""ARAH DESAIN: {direction.label} - {direction.mood}
Font: heading {heading}; teks {body}
Radius: {direction.radius}
Shadow: {direction.shadow}
Elemen khas: {direction.signature}{extra}

Token warna (pakai PERSIS nilai ini sebagai custom properties di :root):
{tokens}

ATURAN VISUAL:
- DILARANG memakai #007bff, #6c757d, atau system-ui sebagai font utama.
- DILARANG tampilan hitam-putih tanpa warna. Warna brand WAJIB terlihat.
- Setiap bagian harus punya jarak vertikal 80-120px di desktop, 48-64px di mobile.
- Pakai skala tipografi jelas: h1 clamp(2.4rem,5vw,4rem), h2 clamp(1.8rem,3.2vw,2.6rem).
- Kartu: latar surface, border 1px, radius, shadow saat hover (transform translateY(-4px)).
- Tombol utama: latar brand, teks putih, padding 14px 28px, radius, hover brand-strong.
- WAJIB ada state :focus-visible dengan outline 2px accent.
- WAJIB ada animasi masuk halus (opacity+translateY) lewat IntersectionObserver,
  dan hormati prefers-reduced-motion.
- Mode gelap: pakai prefers-color-scheme DAN tombol toggle yang benar-benar jalan.
- Responsif nyata: grid 1 kolom di bawah 720px, 2-3 kolom di atasnya.
- Jangan pakai gambar eksternal. Pakai gradien, SVG inline, atau pola CSS."""