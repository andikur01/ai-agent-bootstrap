# AGENTS.md

Bot Telegram AI agent pribadi. Bahasa user utama: Indonesia.

## Setup & test

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium     # wajib untuk QA website
cp .env.example .env            # isi minimal TELEGRAM_BOT_TOKEN + 1 API key LLM
python scripts/doctor.py        # preflight: cek env, tool, runtime
python -m pytest tests/ -q      # 104 test, ~19s
python main.py                  # jalankan bot
```

`scripts/doctor.py` adalah cara tercepat tahu apa yang belum dikonfigurasi.
Test tidak butuh API key nyata; gateway di-stub lewat fixture di `tests/conftest.py`.

## Arsitektur

Alur satu pesan: `route -> resolve reference -> confirm (heavy saja) -> execute -> remember`.

- `app/core/orchestrator.py` — satu tempat semua intent jadi aksi. Handler bernama
  `_do_<intent.value>`. Tambah intent = tambah enum di `app/core/intent.py` +
  handler `_do_*` + entri di `CONFIRM_TEMPLATES` dan `TOKEN_ESTIMATES`.
- `app/core/intent.py` — routing 2 lapis: regex cepat dulu, LLM kecil hanya kalau
  regex ragu. Pola pakai `\w*` di akhir kata kunci (mis. `gambarnya`) karena
  sufiks Indonesia bikin `\b` gagal.
- `app/memory/` — resolver "yang tadi/lanjutkan" + registry artifact + Chroma.
  Intent `CONTINUE` wajib resolve ke artifact nyata, bukan ke chat terakhir.
- `app/services/` — image/video/audio/web/link/n8n. Semua hasil lewat QA dulu.
- `app/llm/gateway.py` — fallback Groq → Gemini → OpenRouter → HF, retry + backoff,
  trim token via tiktoken, catat ke budget.

## Konvensi

- Secret HANYA dari env. `app/security/redact.py` memfilter log, `assert_no_secrets`
  memblokir pesan keluar, `mask_secrets_in_image` mem-blur screenshot sebelum kirim.
- Semua error dicatat ke store (`log_error`) supaya bisa dibaca ulang dan tidak diulang.
- Media/gambar selalu dikirim sebagai file jadi atau link, bukan path mentah.
- Komentar menjelaskan "kenapa", bukan "apa". Hindari narasi diff.

## Jebakan yang sudah pernah kena

- ffmpeg + browser Playwright hilang setelah environment reset. Jalankan
  `scripts/doctor.py` dulu sebelum menyalahkan kode.
- `BrowserQA(outputs_dir: Path)` menerima path, bukan objek Settings.
- `BrowserAgent` menandai elemen dengan `data-agent-idx` lalu mencarinya lagi
  lewat atribut itu. Jangan ganti ke pencocokan teks: begitu dua link punya
  label sama, aksi akan mengenai elemen yang salah.
- Intent CONTINUE jangan sampai jatuh ke `_do_chat`; itu bug lama dan sudah diperbaiki.
- Pollinations sering 503 saat sibuk; retry + fallback render lokal sudah ada.
- `pip install python-telegram-bot` polos bikin startup crash di
  `AIORateLimiter`. Pakai extras: `python-telegram-bot[rate-limiter]`.
- Test harus hermetik: pydantic `Settings` selalu baca `.env` kalau path-nya
  default, jadi `.env` asli bisa membalik asumsi test. `tests/conftest.py`
  menunjuk `ENV_FILE` ke file yang tidak ada.
- Bot fail-closed: token invalid atau allow-list kosong = bot tidak start.
  `main()` sudah memberi pesan singkat (exit 3), bukan traceback mentah.
- Handler command/callback TIDAK lewat `on_message`, jadi allow-list hanya menjaga
  teks bebas. Sekarang ada `on_guard` di group -1 (`app/application.py`) yang
  mengizinkan dulu dan `ApplicationHandlerStop` kalau bukan user. `/start`
  dikecualikan supaya user baru tetap bisa melihat user id-nya sendiri.
- `SMALL_MODEL`/`BIG_MODEL` dulu setting mati: gateway selalu pakai tabel tier
  hardcoded. Sekarang override memimpin tier-nya hanya kalau provider-nya punya
  key, supaya rantai fallback tidak mulai dari model yang tak bisa dipanggil.
- `/menu` isinya data (`MENU_SECTIONS`), bukan string raksasa, supaya bisa diuji
  tanpa koneksi Telegram. `tests/test_menu.py` menguji isi menu, routing tombol,
  alias command, dan guard.

## Pitfalls found in production (read before editing the LLM path)

**Never put reserved LogRecord keys in `extra=`.** `extra={"module": "llm"}`
raises `KeyError: Attempt to overwrite 'module' in LogRecord` on Python 3.13.
Inside `complete()` that KeyError was caught by the provider-failure handler, so
every *successful* reply was reported as a failed provider. Use `component=`.
`tests/test_regressions.py` scans `app/` for reserved keys, so this cannot come
back silently.

**Groq model names rot.** `llama-3.1-8b-instant` and `llama-3.3-70b-versatile`
are gone; Groq now serves `openai/gpt-oss-20b` and `openai/gpt-oss-120b`. Check
`GET https://api.groq.com/openai/v1/models` with a real key before assuming a
model still exists - a dead name fails *every* call with `model_not_found`.

**Reasoning models need output headroom.** `gpt-oss`, `deepseek-reasoner` and
friends spend output tokens on hidden reasoning first. Callers that pass a small
`max_tokens` (the memory resolver uses 60, intent routing similar) get back an
empty string. `_needs_reasoning_floor()` in `gateway.py` raises the cap to
`REASONING_MIN_OUTPUT`, and an empty completion is raised as a retryable error
rather than returned as the answer.

**A provider with a key but absent from `LLM_FALLBACK_ORDER` is ignored.**
`available_providers()` appends such providers after the configured order, so a
key added via `/keys add` always takes effect. Without this the user adds a
DeepSeek key and nothing changes.

## Verifying a new key end to end

```bash
# 1. does the provider accept it?
curl -s -H "Authorization: Bearer $KEY" https://api.groq.com/openai/v1/models
# 2. does the app get a real reply through it?
PYTHONPATH=. .venv/bin/python -c "
import asyncio; from app.core.config import get_settings
from app.application import build_app
async def m():
    app = await build_app(get_settings())
    r = await app.orchestrator.gateway.complete([{'role':'user','content':'Balas: halo'}], tier='small')
    print(r.provider, r.model, repr(r.text)); await app.stop()
asyncio.run(m())"
```

Keys live in `.env` only, which is gitignored. `/keys add <provider> <key>` in
Telegram validates, stores and activates a key without a restart.

## Self-service keys: what is possible and what is not

Providers expose no API for creating API keys. Automating account *signup* is
what gets accounts banned, so the bot never does it. The split that works:

* The human signs in once, in a real browser (`/login <url>`), including 2FA and
  captcha. The password never reaches the bot.
* The bot drives that saved session afterwards (`/keys auto <provider>`): open
  the console, click the create-key button, scrape the key, verify it, store it.

**DISPLAY must be set before `async_playwright()` starts.** The Playwright driver
reads its environment once and hands it to every browser it spawns. Setting
`os.environ["DISPLAY"]` after `async_playwright()` has begun silently does
nothing, and Chromium dies with "Missing X server or $DISPLAY". `_ensure_display()`
is therefore called *before* the `async with`.

**A stale X socket lies.** A crashed Xvfb leaves `/tmp/.X11-unix/X<n>` behind.
Reusing that display fails; skip any number whose socket already exists and let
Xvfb take a fresh one. Confirm readiness by waiting for the socket to appear, not
by trusting `proc.returncode is None`.

**Headless hosts cannot host an interactive login.** Xvfb gives the browser
somewhere to run, and the bot can screenshot it, but the user has no window to
type into. On such a host the practical path is `/keys add <provider> <key>`
(created in the user's own browser). `/login` + `/keys auto` is fully automatic
only where a window can be seen - the user's own machine, or a VPS with VNC.
