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
