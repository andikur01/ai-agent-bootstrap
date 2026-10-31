# AI Agent Bootstrap

Bot Telegram pribadi yang bisa ngobrol dengan ingatan penuh, bikin dan edit
gambar/video/suara, bangun dan perbaiki website, bikin workflow n8n, belajar dari
internet, dan membaca isi link apa pun.

Dibangun untuk jalan hemat token: gateway multi-provider dengan fallback, cache
semantik, memori ringkas, dan pemotongan prompt berbasis tiktoken.

---

## Isi cepat

- [Fitur](#fitur)
- [Arsitektur](#arsitektur)
- [Instalasi](#instalasi)
- [Konfigurasi](#konfigurasi)
- [Cara pakai](#cara-pakai)
- [Hemat token](#hemat-token)
- [Keamanan](#keamanan)
- [Test](#test)
- [Deploy](#deploy)
- [Struktur proyek](#struktur-proyek)
- [Troubleshooting](#troubleshooting)

---

## Fitur

| Area | Yang bisa dilakukan |
| --- | --- |
| Chat | Ngobrol natural, ingat seluruh riwayat, paham "perbaiki yang tadi" / "lanjutkan itu" / "yang kemarin" |
| Pikir | `/pikir <pertanyaan>` - penalaran 5 pass (frame, plan, work, check, answer) |
| Gambar | Generate (HuggingFace FLUX lalu Pollinations) dan edit (instruct-pix2pix, Replicate, atau pipeline Pillow) |
| Video | Bikin dari naskah: motion graphics Manim atau slideshow Pillow+FFmpeg dengan narasi; edit: potong, gabung, speed, aspect, subtitle, musik |
| Audio | TTS (edge-tts, suara Indonesia), STT (Groq Whisper lalu faster-whisper), edit: speed, volume, trim, denoise, musik |
| Web static | Bikin HTML/CSS/JS modern, deploy ke Cloudflare Pages atau GitHub Pages |
| Web fullstack | FastAPI + Supabase (login + database), deploy ke Render |
| Web repair | Buka hasil deploy di Playwright, screenshot, cek console error, perbaiki, deploy ulang |
| n8n | Bikin workflow dari deskripsi, import lewat API n8n |
| Belajar | `/belajar <topik>` - cari di internet, rangkum, simpan permanen |
| Link | Kirim link YouTube/TikTok/IG/artikel, bot baca isinya dan jawab pertanyaanmu |
| Jadwal | Reminder sekali atau berulang, tersimpan di SQLite |

---

## Arsitektur

```
Telegram  ──▶  handlers.py  ──▶  orchestrator.py  ──▶  services/*
                  │                   │
                  │                   ├─ llm/gateway.py     (fallback provider, budget)
                  │                   ├─ llm/cache.py       (cache semantik)
                  │                   ├─ memory/*           (Chroma + artifact registry)
                  │                   └─ services/*         (gambar, video, audio, web, n8n, ...)
                  │
                  └─ progress.py (persen + screenshot milestone)
```

Alur satu pesan:

```
route → resolve referensi → konfirmasi (kalau berat) → eksekusi → simpan ke memori
```

Poin desain yang penting:

- **Satu tempat untuk dependency.** `app/application.py` membangun seluruh graph.
  Test bisa membangun graph yang sama dengan gateway palsu tanpa menyentuh Telegram.
- **Model tidak pernah menjalankan kode.** Untuk video, Manim, dan n8n, model
  mengembalikan JSON; kode yang dijalankan dibangun dari daftar putih.
- **Semua tugas berat lewat gerbang konfirmasi** beserta estimasi token.

---

## Instalasi

Butuh Python 3.11+ dan FFmpeg.

```bash
git clone <repo-url> ai-agent-bootstrap
cd ai-agent-bootstrap

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# opsional tapi disarankan: browser untuk QA website
playwright install chromium

cp .env.example .env
# isi minimal TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS, dan satu API key LLM

python scripts/doctor.py       # cek apa saja yang sudah siap
python main.py
```

FFmpeg wajib ada untuk semua pekerjaan media:

```bash
sudo apt-get install -y ffmpeg espeak-ng fonts-dejavu-core    # Debian/Ubuntu
brew install ffmpeg espeak-ng                                  # macOS
```

---

## Konfigurasi

Semua konfigurasi lewat environment variable atau `.env`. Tidak ada secret di
dalam kode. Lihat `.env.example` untuk daftar lengkap.

### Wajib

| Variabel | Keterangan |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token bot dari [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_USER_IDS` | ID Telegram kamu (dari [@userinfobot](https://t.me/userinfobot)). Bot **menolak semua orang** kalau ini kosong |
| `GROQ_API_KEY` atau `GEMINI_API_KEY` atau `OPENROUTER_API_KEY` | Minimal satu provider LLM |

### Opsional, per fitur

| Variabel | Untuk apa | Kalau kosong |
| --- | --- | --- |
| `TAVILY_API_KEY` | Riset lebih bagus | fallback DuckDuckGo (tanpa key) |
| `SEARXNG_BASE_URL` | Meta search sendiri | dilewati |
| `HF_TOKEN` | Gambar via FLUX | fallback Pollinations (tanpa key) |
| `REPLICATE_API_TOKEN` | Video AI, edit gambar lanjutan | jalur lokal dipakai |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | Deploy static tercepat | fallback GitHub Pages |
| `GITHUB_TOKEN`, `GITHUB_OWNER` | GitHub Pages + repo untuk Render | deploy static dilewati |
| `RENDER_API_KEY`, `RENDER_OWNER_ID` | Deploy web fullstack | QA lokal + bundle zip |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | Login + database web app | app jalan tanpa auth |
| `N8N_BASE_URL`, `N8N_API_KEY` | Import workflow otomatis | JSON disimpan ke `outputs/n8n/` |
| `YTDLP_COOKIES_FILE` | Link yang butuh login | platform yang butuh cookie gagal |
| `YOUTUBE_CLIENT_SECRET_FILE`, `YOUTUBE_TOKEN_FILE` | Fallback upload privat | jalur itu dilewati |

### Batas dan tuning

| Variabel | Default | Keterangan |
| --- | --- | --- |
| `DAILY_TOKEN_BUDGET` | `200000` | Batas token per user per hari |
| `SEMANTIC_CACHE_ENABLED` | `true` | Cache jawaban mirip |
| `RATE_LIMIT_PER_MINUTE` | `20` | Pesan per user per menit |
| `HEAVY_TASK_DAILY_LIMIT` | `25` | Kuota tugas berat per hari |
| `LLM_FALLBACK_ORDER` | `groq,gemini,openrouter,hf,openai` | Urutan fallback |
| `SMALL_MODEL` | `groq/llama-3.1-8b-instant` | Untuk chat dan klasifikasi |
| `BIG_MODEL` | `groq/llama-3.3-70b-versatile` | Untuk tugas berat |

---

## Cara pakai

Cukup ngobrol seperti biasa, atau pakai perintah:

| Perintah | Fungsi |
| --- | --- |
| `/start`, `/bantu` | Daftar kemampuan |
| `/belajar <topik>` | Riset + simpan ke memori permanen |
| `/pikir <pertanyaan>` | Analisis 5 pass |
| `/memori` | Statistik memori dan token |
| `/feedback <pesan>` | Catat masukan |
| `/jadwal` | Lihat reminder aktif |
| `/bataljadwal <id>` | Batalkan reminder |
| `/batal` | Batalkan konfirmasi yang menunggu |
| `/admin` | Panel admin (khusus `TELEGRAM_ADMIN_USER_IDS`) |

Contoh percakapan:

```
Kamu: buatkan gambar kucing astronot
Bot:  🎨 Bikin gambar baru?
      Estimasi: ~900 token
      Balas ya untuk lanjut, tidak untuk batal.
Kamu: ya
Bot:  [gambar]
      Gambar jadi (pollinations, 1024x1024).

Kamu: edit gambarnya jadi nuansa malam
Bot:  [gambar hasil edit]

Kamu: bikin video animasi 3 langkah mencuci tangan
Bot:  [video + narasi]

Kamu: buat website untuk kedai kopi
Bot:  [screenshot desktop + mobile]
      QA lolos setelah 1 putaran perbaikan.
      URL: https://....

Kamu: https://youtu.be/xxxx jelaskan isinya
Bot:  🎬 <judul>
      (subtitles)
      <jawaban>

Kamu: lanjutkan yang tadi
Bot:  <melanjutkan pekerjaan yang benar, bukan chat terakhir>
```

---

## Hemat token

Lapisan yang membuat ini tidak boros:

1. **Fallback provider.** Kalau Groq kena limit, otomatis pindah ke Gemini,
   OpenRouter, HF, lalu OpenAI. Satu provider mati bukan masalah.
2. **Cache semantik.** Pertanyaan yang mirip dijawab dari cache, nol token.
3. **Memori ringkas.** Hanya ringkasan dan potongan relevan yang dikirim ke LLM,
   bukan seluruh riwayat chat.
4. **Pemotongan tiktoken.** Prompt yang melebihi budget dipangkas sebelum dikirim.
5. **Model kecil dulu.** Chat dan klasifikasi pakai model kecil; model besar hanya
   untuk tugas berat.
6. **Estimasi sebelum eksekusi.** Tugas berat menampilkan estimasi token di
   gerbang konfirmasi.

Cek pemakaian dengan `/memori`.

---

## Keamanan

- Semua secret hanya di `.env`, GitHub Secrets, atau HF Secrets. `.env` sudah
  masuk `.gitignore`.
- Lapisan redaksi menyaring pesan keluar, log, dan screenshot. Nilai secret
  dikumpulkan dari config dan diganti `***REDACTED***` sebelum keluar.
- Screenshot QA bisa di-blur otomatis pada area yang rawan (`mask_secrets_in_image`).
- Bot **fail-closed**: allow-list kosong berarti tidak ada yang bisa pakai.
- Tidak ada pembuatan akun otomatis, tidak ada penyelesaian CAPTCHA, tidak ada
  OAuth otomatis. Semua login/izin dilakukan manual oleh kamu.
- `data/`, `outputs/`, dan file token sudah masuk `.gitignore`.

---

## Test

```bash
pytest                      # semua test
pytest -v                   # detail
pytest tests/test_services.py -k n8n
```

Test yang ada benar-benar menjalankan jalur nyata: FFmpeg, Pillow, Playwright,
generator n8n, dan parser reminder. Tidak ada mock untuk logika sendiri.

```
85 passed
```

---

## Deploy

Panduan lengkap (Hugging Face Spaces dan VPS) ada di [DEPLOY.md](DEPLOY.md).
Ringkasnya:

**Docker Compose (paling mudah):**

```bash
cp .env.example .env    # isi secret
docker compose up -d --build
docker compose logs -f bot
```

**Hugging Face Spaces:** buat Space bertipe Docker, unggah repo, isi secret di
Settings, dan set `TELEGRAM_BOT_TOKEN` serta `TELEGRAM_ALLOWED_USER_IDS`.

---

## Struktur proyek

```
ai-agent-bootstrap/
├── app/
│   ├── main.py                # entrypoint
│   ├── application.py         # wiring semua komponen
│   ├── bot/
│   │   ├── handlers.py        # handler Telegram: auth, gate, progress
│   │   └── presenter.py       # kirim teks/media, split, redaksi
│   ├── core/
│   │   ├── config.py          # semua setting dari env
│   │   ├── intent.py          # router intent (rule + LLM)
│   │   ├── orchestrator.py    # route → konfirmasi → eksekusi → memori
│   │   ├── thinking.py        # penalaran 5 pass
│   │   ├── progress.py        # progress bar + gerbang konfirmasi
│   │   ├── errors.py          # hierarki error dengan pesan user
│   │   ├── logging_setup.py   # logging + filter redaksi
│   │   └── utils.py
│   ├── db/store.py            # SQLite: user, pesan, artifact, task, error, jadwal
│   ├── llm/
│   │   ├── gateway.py         # LiteLLM, fallback, retry, pemotongan
│   │   ├── budget.py          # penjaga budget token harian
│   │   ├── cache.py           # cache semantik
│   │   └── tokens.py          # hitung token tiktoken
│   ├── memory/
│   │   ├── manager.py         # API memori tingkat tinggi
│   │   ├── vector_store.py    # Chroma + fallback SQLite
│   │   ├── resolver.py        # resolusi "yang tadi"
│   │   ├── artifacts.py       # registry artifact
│   │   └── embeddings.py      # embedder
│   ├── security/redact.py     # redaksi secret
│   └── services/
│       ├── image_service.py   video_service.py   audio_service.py
│       ├── web_builder.py     web_repair.py      deploy_service.py
│       ├── browser_qa.py      search_service.py  link_service.py
│       ├── n8n_service.py     scheduler_service.py
│       ├── ffmpeg.py          media_qa.py        youtube_upload.py
├── scripts/
│   ├── doctor.py              # cek konfigurasi
│   └── youtube_auth.py        # OAuth YouTube manual
├── tests/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── DEPLOY.md
```

---

## Troubleshooting

**Bot diam saja.**
Cek `TELEGRAM_ALLOWED_USER_IDS` sudah berisi ID kamu, lalu lihat log:
`docker compose logs -f bot`.

**"Butuh provider LLM"**
Isi minimal satu API key LLM di `.env`, lalu jalankan `python scripts/doctor.py`.

**Gambar jelek atau gagal.**
Isi `HF_TOKEN` untuk FLUX, atau `REPLICATE_API_TOKEN`. Tanpa keduanya, bot pakai
Pollinations yang kualitasnya bervariasi. Bot otomatis mengulang sampai 3 kali
sebelum menyerah.

**Video gagal.**
Pastikan `ffmpeg` ada di PATH. Untuk motion graphics, `manim` juga harus
terpasang (`pip install manim`, butuh cairo dan pango).

**Website tidak ter-deploy.**
Isi kredensial deploy, atau bot akan memberi bundle zip di `outputs/web/`.

**Link diblokir.**
Isi `YTDLP_COOKIES_FILE` dengan cookies browser kamu, atau kirim file videonya
langsung ke bot.

**QA website gagal terus.**
Jalankan `playwright install chromium`. Screenshot hasil QA ada di
`outputs/qa/` supaya bisa dilihat langsung.

---

## Lisensi

MIT. Pakai dan ubah sesuai kebutuhan.
