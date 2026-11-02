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

**A headless host still has a window, if you build one.** Xvfb alone gives the
browser somewhere to run but the user nowhere to type. The fix is the rest of the
stack: x11vnc shares that display, websockify serves noVNC over the published
port, and the user opens the viewer URL from their phone. Then `/login` works
exactly as it does on a desktop, and `/keys auto` can run unattended afterwards.

This was missed for a long time because the four OS packages were never
installed, so `live_view.ensure()` reported the stack unavailable and the failure
looked like a code bug. `scripts/setup_system.sh` installs and verifies them, and
the Dockerfile installs the same set. The display now starts at boot rather than
on first use, because a screen that only appears once a task needs it is a screen
the user cannot open while waiting.

The VNC password is generated once and kept in the gitignored data directory, so
it survives restarts; `LIVE_VIEW_PASSWORD` overrides it.

**"Ambilkan key" is a first-class request.** The whole reason the browser and
the harvester exist is so the user never opens a provider console. Routing that
only through `/keys auto` defeats it, so `_parse_harvest_request` in
`app/bot/handlers.py` recognises plain phrasing ("ambilkan key gemini",
"ambil semua key") and sends it straight to the harvester. Account creation is
excluded on purpose - automating signup is what gets accounts banned, so that
stays manual. When no provider is named, try all of them and report which ones
still need a login; a useful partial answer beats a question bounced back.

## Generated sites: three traps that each look like a different bug

Every one of these made a deployed site fail its own quality gate, and each
presented as a different symptom, so they are worth stating plainly.

**aiohttp hands the handler the request, not the captured path.** The route was
`/s/{name}/{path:.*}` with a handler `_serve_file(self, request, path="")`. The
`{path}` group never reached the parameter, so `path` was always the default and
every request - `/styles.css`, `/script.js` - was answered with `index.html`.
Sites loaded unstyled with no JavaScript. Read `request.match_info["path"]`.

**Models sometimes double-escape file contents.** The model returns valid JSON
whose string values are themselves escaped, so a newline arrives as the two
characters `\n`. The JSON parses, nothing raises, and the written file is one
long line - which the browser reports as "Invalid or unexpected token" for JS.
`_decode_double_escaped` in `app/services/web_builder.py` undoes that second
round, but only when the whole file is escaped and `\n` never appears as real
newlines, so genuine backslashes survive. Encode `latin-1` with
`backslashreplace` first, or `unicode_escape` mojibakes every emoji.

**Browsers request `/favicon.ico` whether or not the page links it.** A 404
lands in the console as "Failed to load resource" and failed QA on every site.
Serve a default icon.

Two more, from the quality gate and the LLM gateway:

**A short page full of controls is not an empty page.** Judging content by
visible text length alone flagged every login or dashboard page as "konten
terlalu sedikit" and sent the repair loop after a defect that did not exist.
`PageReport.ok` now also counts interactive elements.

**"Request too large" is not a rate limit on the key.** The free Groq tier caps
at 8000 tokens per minute, and a full web-build prompt of ~11k was rejected
every time. The gateway used to bench and rotate a perfectly healthy key, which
could never help, because the limit is on the prompt. It now trims the prompt
and the output reservation, then retries: verified live, 12530 tokens ->
shrink->3000 -> ok.

**Deploy provider order matters, and the local host is last.** The runtime
publishes two ports. `SiteHost` serves generated sites there and needs no token,
which is the only provider that works with a fine-grained GitHub token that
cannot create repositories. Reuse the host started at boot - a second one tries
to bind a port that is already taken and fails silently.

**A one-time-code screen is not a sign-in wall, and treating it as one loses the
task.** The harvester tested for the wall by looking for sign-in wording, so an
OTP page - which says "verify", not "sign in" - was read as "user is not logged
in" and the task ended one step short of the key. Detect the code screen by its
input field (`_needs_verification`), and hand the code to the user through
`HandoffService` instead of stopping.

**A question to the user is not a confirmation.** `ConfirmationGate` answers
yes/no and is checked before a task starts; a verification code arrives *while*
a task is running and is free text. They are separate services for that reason.
Route the reply in `on_message` before the confirmation gate, and only consume
a message when someone is actually waiting - a slash command must still route,
or a user who changes their mind is stuck answering a question forever.

**A missing import is a dead bot, and only a running bot reveals it.** Two
crashes reached the user because no test imports the module in the way the
process does: `asyncio` was used in `application.py` without an import (killed
startup), and `bind_task` called `.set` on `_TASK_BINDING`, which is a factory,
not a `ContextVar` (killed every chat). Both were invisible to a green suite
because the pipeline tests passed `cost_guard=None`, so the broken line never
ran. Lesson: after wiring, start the real process and read its log, and give
every production-only path at least one test that runs it. `deploy/run-forever.sh`
caught the first one within 15 seconds - the watchdog is the reason the failure
was visible instead of silent.

**Telegram bots cannot stream video, so "watch it work" is a moving image, not
a broadcast.** The honest implementation is a GIF that is edited in place every
few seconds (`app/services/live_feed.py`), plus `/live` for full noVNC control.
Editing needs a real `Bot` object; a plain `Message` double has none, so the
feed falls back to posting a new tile instead of raising.

**A preference written before the first message used to vanish.** `set_pref`
ran an `UPDATE ... WHERE user_id = ?` that matched zero rows when the user had
never been stored, so `/siaran off` silently did nothing. Insert the row first.

**A cost guard whose default is "unlimited" is not a cost guard.** The daily
budget defaulted to `DAILY_TOKEN_BUDGET=0`, the ceiling lived only in `.env` on
the server, and `CostGuard.unlimited_warning` was never called - so the first
signal the user got was a ~1 million token day. Default to a real number
(150000), surface the ceiling in `/mode` and `/budget`, and let `/budget <n>`
change it from the chat. A limit the user cannot see or move from their phone
is not a limit, it is a surprise.

**Writing a preference is not the same as applying it.** `credentials.append_key`
appends, which is right for numbered API key slots but wrong for a tuning value:
two `/budget` calls would have left two `DAILY_TOKEN_BUDGET` lines and the last
one wins only by luck. `set_env_var` replaces by regex. Reach for append for
keys, replace for settings.

**A browser that clicks invisibly looks broken to the person watching.** The
agent's clicks left no trace on the shared screen, so pages changed by
themselves. A cosmetic fixed-position pointer, slid to the target before each
click and type, fixes it without touching the element list the model picks
from. Keep it out of `_INTERACTIVE` and swallow its errors - a cursor is never
worth failing a task over.

**A login wall is a question, not a dead end.** The agent used to stop when it
had no saved account. It now asks for one mid-task through the same
`HandoffService` used for verification codes, parses the answer with or without
the host, stores it, and signs in without restarting. The bot deletes the
credential message after consuming it - but only when it actually looks like a
login (`_looks_like_credentials`), so a one-time code is not deleted.

**An invisible feature is not a feature.** Task routing to specialists existed,
but nothing in the reply said who handled it, so the user could not tell it
worked. Name the expert on every non-chat reply. Likewise, do not write
"terenkripsi" when the file is only mode 0600 - say what is actually true.

**A budget only holds on the paths it can see.** The daily token limit looked
enforced because the gateway refused overspending - but browser-use calls the
provider directly, so every browsing run spent tokens the meter never recorded.
That is the shape of most cost bugs: not a missing limit, a path around it.
Any component that talks to a provider outside the gateway must check the
remaining budget before it starts and record what it spends afterwards, failure
included. Verify by running the real component and comparing the meter's delta
to the reported usage, not by reading the code.

**Check what a data source actually contains before building on its name.**
"Get the key from public-apis" fails as written: that repo has no keys, only a
directory of ~2,200 services and where each issues one. The feature that works
is the step before the key - which services exist, which need a credential, and
where to sign up - then hand off to the existing harvester. When a request names
a source, read the source first and say plainly what it is and is not.

**Match the data's language, not the user's.** The public-apis directory is in
English and the user types Indonesian, so "cari api cuaca" matched nothing and
the feature looked broken on first use. A small synonym map fixes it; the
general lesson is that a search over a fixed corpus needs the query normalised
into the corpus's own vocabulary before scoring.

**Test isolation is not optional when tests read the real environment.** The
suite loads `.env`, so a real `TELEGRAM_ALLOWED_USER_IDS`, `GROQ_API_KEY`, or
VNC host in the shell makes unrelated tests fail with "user not authorised" or
a non-empty viewer URL. Before blaming a change, re-run the failing tests with
those variables unset; four of twenty baseline failures were exactly this.

**A fixed port is a single point of failure on a shared host.** x11vnc failed
to start with "Address already in use" on 5900 even though nothing appeared to
own it, which took the whole live screen down. Take the first free port for
internal helpers; keep only the port the user opens fixed, because moving that
one hands them a URL nothing serves.

**Do not trust `ss`, `lsof`, or `os.kill(pid, 0)` for process state here.**
`ss` is not installed, so shelling out to it silently found no port owners and
the stale-helper sweep never ran. `os.kill(pid, 0)` succeeds for a zombie, and
the watchdog leaves the killed bot as a zombie, so the sweep skipped exactly
the leftovers it exists to clear. Read `/proc/net/tcp` for listening sockets
and `/proc/<pid>/stat` state for liveness.

**A screenshot is not proof the screen shows anything.** Measure image variety
before claiming the live view works: a blank Xvfb capture is one colour, a
rendered page is hundreds. The feed also needs `scrot` in the image, or it
falls back to QA stills and the user sees only a dot.
