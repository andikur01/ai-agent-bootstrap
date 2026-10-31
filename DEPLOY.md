# Deploy

Dua jalur: Hugging Face Spaces (paling cepat, gratis) dan VPS (paling fleksibel).
Keduanya pakai Docker yang sama.

Sebelum mulai, siapkan tiga hal ini:

1. `TELEGRAM_BOT_TOKEN` dari [@BotFather](https://t.me/BotFather)
2. `TELEGRAM_ALLOWED_USER_IDS` (ID Telegram kamu, dari
   [@userinfobot](https://t.me/userinfobot))
3. Minimal satu API key LLM (`GROQ_API_KEY` paling gampang, gratis)

---

## 1. Hugging Face Spaces (mode Docker)

Gratis, tapi storage sementara: data hilang saat Space di-restart. Cocok untuk
coba-coba atau pemakaian ringan.

### Langkah

1. Buat Space baru di <https://huggingface.co/new-space>:
   - **SDK**: Docker
   - **Template**: Blank
   - **Visibility**: Private (lebih aman)

2. Unggah isi repo ini ke Space. Paling mudah lewat git:

   ```bash
   git clone https://huggingface.co/spaces/<username>/<space-name>
   cd <space-name>
   # copy semua file proyek ke sini
   git add .
   git commit -m "deploy bot"
   git push
   ```

3. Tambahkan secret di Space: **Settings → Variables and secrets**. Tambahkan
   sebagai *Secret* (bukan Variable publik):

   ```
   TELEGRAM_BOT_TOKEN
   TELEGRAM_ALLOWED_USER_IDS
   TELEGRAM_ADMIN_USER_IDS      (opsional)
   GROQ_API_KEY                 (atau provider lain)
   ```

   Tambahkan yang lain sesuai fitur yang kamu mau: `TAVILY_API_KEY`,
   `HF_TOKEN`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `GITHUB_TOKEN`,
   `GITHUB_OWNER`, `RENDER_API_KEY`, `RENDER_OWNER_ID`, `SUPABASE_URL`,
   `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `N8N_BASE_URL`,
   `N8N_API_KEY`, `REPLICATE_API_TOKEN`.

4. Space akan build dan menjalankan `python main.py`. Cek log di tab **Logs**.

5. Kirim pesan ke botmu. Kalau tidak merespons, cek `TELEGRAM_ALLOWED_USER_IDS`.

### Catatan HF Spaces

- Space gratis punya batas CPU/RAM. Untuk video Manim yang berat, naikkan ke
  Space berbayar atau pindah ke VPS.
- Chroma dan SQLite berjalan lokal di dalam Space. Data tidak persisten.
  Kalau butuh persisten, pakai VPS, atau set `CHROMA_HOST` ke layanan eksternal.
- Jangan taruh token di file `.env` yang di-commit. Selalu pakai Secrets.

### Dockerfile untuk Space

Dockerfile di repo sudah kompatibel dengan HF Spaces. Kalau Space menolak build
karena user non-root, hapus dua baris terakhir sebelum `CMD`:

```dockerfile
# RUN useradd --create-home --shell /bin/bash agent \
#     && chown -R agent:agent /app
# USER agent
```

---

## 2. VPS (persisten, direkomendasikan)

Butuh VPS dengan minimal 2 GB RAM (4 GB kalau mau Manim dan Whisper lokal).

### Prasyarat

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER    # logout lalu login lagi
```

### Deploy

```bash
git clone <repo-url> ai-agent-bootstrap
cd ai-agent-bootstrap
cp .env.example .env
nano .env                        # isi secret
docker compose up -d --build
docker compose logs -f bot
```

Yang akan jalan:

| Service | Port (localhost) | Fungsi |
| --- | --- | --- |
| `bot` | - | Bot Telegram |
| `chroma` | 8001 | Vector store |
| `n8n` | 5678 | Editor n8n |

Semua port di-bind ke `127.0.0.1` saja, jadi tidak terbuka ke internet. Untuk
mengakses n8n, pakai SSH tunnel:

```bash
ssh -L 5678:localhost:5678 user@vps-ip
# lalu buka http://localhost:5678 di browser
```

### Dapatkan API key n8n

1. Buka n8n lewat tunnel, buat akun admin (manual, oleh kamu).
2. **Settings → n8n API → Create an API key**.
3. Salin ke `.env` sebagai `N8N_API_KEY`, lalu:

   ```bash
   docker compose up -d --force-recreate bot
   ```

Sekarang "buatkan workflow n8n untuk ..." akan langsung mengimpor workflow.

### Persistensi

Volume yang dipakai:

- `./data` - SQLite, memori, token YouTube, upload
- `./outputs` - gambar, video, audio, website, screenshot QA
- `chroma-data` - vector store
- `n8n-data` - workflow dan kredensial n8n

Backup cukup dengan `tar` folder `data` dan `outputs`:

```bash
tar czf backup-$(date +%F).tar.gz data outputs
```

### Update

```bash
git pull
docker compose up -d --build
```

---

## 3. Setup layanan pendukung

### Supabase (untuk web app login + database)

1. Buat project di <https://supabase.com> (manual).
2. Ambil dari **Project Settings → API**: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
   `SUPABASE_SERVICE_ROLE_KEY`.
3. Isi ke `.env` atau Secrets.
4. Saat bot membuat web app, ia akan memberi daftar tabel yang perlu dibuat.
   Buat tabel itu di **SQL Editor** Supabase.

### Cloudflare Pages (deploy static tercepat)

1. Buat API token di **My Profile → API Tokens** dengan izin
   `Account → Cloudflare Pages → Edit`.
2. Ambil Account ID dari halaman overview dashboard.
3. Isi `CLOUDFLARE_API_TOKEN` dan `CLOUDFLARE_ACCOUNT_ID`.

### GitHub Pages

1. Buat token di **Settings → Developer settings → Personal access tokens**
   dengan scope `repo`.
2. Isi `GITHUB_TOKEN` dan `GITHUB_OWNER` (username kamu).

### Render (web fullstack)

1. Buat API key di **Account Settings → API Keys**.
2. Ambil Owner ID dari URL dashboard (`.../o/<owner-id>/...`).
3. Isi `RENDER_API_KEY` dan `RENDER_OWNER_ID`.

### YouTube (fallback link yang diblokir)

Login manual, sekali, di mesinmu sendiri:

```bash
python scripts/youtube_auth.py
```

Token disimpan ke `data/youtube_token.json` (mode 600, sudah di `.gitignore`).
Bot hanya membaca file itu; bot tidak pernah menjalankan OAuth sendiri dan tidak
pernah membuat akun.

---

## 4. Verifikasi setelah deploy

```bash
docker compose exec bot python scripts/doctor.py
```

Outputnya menunjukkan apa yang sudah siap dan apa yang belum. Lalu kirim
`/start` ke botmu di Telegram.

---

## 5. Kalau ada masalah

| Gejala | Penyebab umum | Solusi |
| --- | --- | --- |
| Build gagal di langkah `pip install` | Memori kurang saat build Manim | Build di mesin lain lalu push image, atau naikkan RAM |
| Bot tidak merespons | Allow-list kosong atau salah | Isi `TELEGRAM_ALLOWED_USER_IDS` |
| "Butuh provider LLM" | Tidak ada API key LLM | Isi minimal satu |
| Video gagal | FFmpeg tidak ada | Image sudah termasuk; kalau jalan native, install `ffmpeg` |
| QA website selalu gagal | Chromium tidak terpasang | `playwright install chromium` di dalam container |
| n8n import gagal | API key salah atau n8n belum siap | Tunggu n8n selesai start, cek `N8N_BASE_URL` |
| Link diblokir | Platform minta login | Isi `YTDLP_COOKIES_FILE` atau kirim filenya langsung |

Log:

```bash
docker compose logs -f bot        # realtime
docker compose logs --tail=200 bot
```

---

## 6. Checklist keamanan sebelum produksi

- [ ] `.env` tidak pernah di-commit (`git status` bersih dari `.env`)
- [ ] Space/repo diset **private**
- [ ] `TELEGRAM_ALLOWED_USER_IDS` hanya berisi orang yang kamu izinkan
- [ ] Port n8n dan Chroma tidak terbuka ke internet (pakai SSH tunnel)
- [ ] `N8N_ENCRYPTION_KEY` di `.env` sudah diganti dari default
- [ ] Backup rutin untuk `data/` dan `outputs/`
- [ ] `DAILY_TOKEN_BUDGET` diset sesuai kantongmu
