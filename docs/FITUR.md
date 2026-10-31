# Status Fitur

Diperbarui: 2026-10-31. Status di sini berdasarkan hasil uji nyata, bukan asumsi.

Kolom **Butuh** = hal yang harus ada supaya fitur jalan. Kalau kosong, fitur jalan
tanpa API key (gratis).

## Siap dipakai

| Fitur | Cara pakai | Butuh | Bukti uji |
|---|---|---|---|
| Sambung & mulai bot | `/start`, `/help`, `/bantu` | Token Telegram | Dijalankan end-to-end lawan server Telegram lokal: balasan terkirim |
| Menu semua fitur | `/menu` | - | Teks + tombol inline per kategori; klik tombol mengedit pesan di tempat |
| Status bot & model | `/status`, `/status_model` | - | Menampilkan provider, model aktif, tier kecil/besar |
| Command alias fitur | `/buat_gambar`, `/buat_web`, `/ingat`, dst | - | 12 alias diuji: masing-masing memetakan ke intent yang benar |
| Statistik memori & token | `/memori` | - | Balasan nyata: "Pesan tersimpan: 4, Token hari ini 0/200.000" |
| Router intent (bahasa Indonesia) | otomatis | - | 13 contoh perintah dirutekan benar tanpa LLM |
| Buat gambar | "buatkan gambar ..." | - | Pollinations gratis; kalau rate-limit (402) jatuh ke render lokal, tetap dapat PNG 1024x1024 |
| Edit gambar | "perbaiki gambarnya jadi gelap" | - | Pipeline lokal Pillow/FFmpeg: brightness, kontras, crop, teks |
| Buat suara (TTS) | "bacakan ini jadi suara" | - | edge-tts gratis, hasil mp3 39 KB |
| Buat video | "bikin video ..." | - | Slideshow/Manim + FFmpeg, mp4 vertikal |
| Edit video | "potong jadi 10 detik" | - | FFmpeg: potong, gabung, subtitle, musik, speed |
| Ingat & rujuk artifact | "lanjutkan yang tadi" | - | Resolve ke artifact terakhir, lalu benar-benar bertindak |
| Pengingat/jadwal | `/ingatkan besok jam 7 olahraga` | - | APScheduler + SQLite |
| Unduh link | kirim link YouTube/TikTok/IG | yt-dlp (terpasang) | Kode + fallback cookies/file |
| Keamanan | otomatis | - | Log ter-redaksi, screenshot di-blur, `.env` ter-gitignore |
| QA website | otomatis | Playwright | Screenshot desktop 1440px + mobile 390px, cek console error |

## Butuh API key (kalau kosong, bot balas pesan error yang jelas)

| Fitur | Butuh | Catatan |
|---|---|---|
| Chat natural & brainstorming | 1 API key LLM | Groq paling murah/cepat |
| `/pikir` (analisis 5 pass) | 1 API key LLM | - |
| Ringkasan `/belajar` | 1 API key LLM | Pencarian web-nya gratis (DuckDuckGo), yang butuh key itu merangkum |
| Jawab pertanyaan tentang link | 1 API key LLM | Unduh + transkrip jalan; menjawab butuh LLM |
| Voice-to-text (STT) | `GROQ_API_KEY` **atau** `pip install faster-whisper` | Groq paling cepat |
| Buat website (static/fullstack) | 1 API key LLM | Generate kode butuh LLM; QA-nya sudah gratis |
| Deploy web jadi link publik | Cloudflare Pages atau GitHub Pages atau Render | Tanpa ini, hasil dikirim sebagai file zip |
| Web app login + database | `SUPABASE_*` + Render | - |
| Import workflow n8n | Instance n8n + `N8N_*` | Tanpa ini, workflow dikirim sebagai file JSON |
| Video AI | `REPLICATE_API_TOKEN` | Kalau kosong, video dibuat dari slideshow/Manim |
| Gambar FLUX kualitas tinggi | `HF_TOKEN` | Kalau kosong, pakai Pollinations gratis |
| Riset lebih bagus | `TAVILY_API_KEY` | Kalau kosong, pakai DuckDuckGo gratis |
| Link yang diblokir | `YTDLP_COOKIES_FILE` | Fallback: user kirim file, atau upload YouTube privat |

## Yang perlu kamu lakukan supaya bisa uji sekarang

1. Telegram: `@BotFather` -> `/mybots` -> pilih bot -> **Revoke token** -> ambil token baru.
2. Groq: console.groq.com/keys -> hapus key lama -> **buat key baru**.
3. Kirim dua nilai baru itu, aku update `.env` dan jalankan sampai polling aktif.
4. Buka Telegram, klik **Start** pada bot kamu, lalu coba:
   - `/start` (harus langsung dibalas)
   - `/memori`
   - `buatkan gambar kucing astronot`
   - `bacakan halo dunia jadi suara`
   - `lanjutkan yang tadi`

Langkah 4 akan langsung jalan begitu token Telegram valid. Empat perintah terakhir
juga tidak butuh key Groq, jadi bisa dites lebih dulu walau key LLM belum siap.
