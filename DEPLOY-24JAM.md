# Cara bikin bot hidup 24 jam (dan tidak penuh)

Kamu bilang: *"tadi pagi saya pakai HP, chat nggak dibalas, berarti belum aktif
24 jam."* Benar. Dan ada **dua** sebab, bukan satu.

---

## Sebab 1: bot-nya memang mati

Aku cek, prosesnya sudah tidak ada. Alasannya: bot dijalankan pakai `nohup`
tanpa penjaga. Begitu sesi berakhir atau server restart, bot ikut mati dan
**tidak ada yang menyalakannya lagi.**

**Perbaikan:** ada 2 cara menyalakan penjaga.

### Cara A: systemd (kalau punya VPS, ini yang terbaik)

```bash
sudo cp deploy/ai-agent.service /etc/systemd/system/
sudo nano /etc/systemd/system/ai-agent.service   # sesuaikan path & User

sudo systemctl daemon-reload
sudo systemctl enable ai-agent      # nyala otomatis saat server reboot
sudo systemctl start ai-agent
sudo systemctl status ai-agent      # cek hidup
```

`Restart=always` bikin bot nyala lagi sendiri kalau crash, kena OOM, atau
server reboot. **Ini yang bikin 24 jam.**

Lihat log kapan saja:
```bash
journalctl -u ai-agent -f
```

### Cara B: runner script (kalau tidak bisa pakai systemd)

```bash
bash deploy/run-forever.sh
```

Script ini restart sendiri, dengan backoff. Kalau bot mati 5x berturut-turut
dalam waktu singkat, dia berhenti dan bilang — karena biasanya itu salah
konfigurasi, bukan gangguan sementara.

Supaya jalan terus walau terminal ditutup:
```bash
tmux new -s bot
bash deploy/run-forever.sh
# tekan Ctrl+B lalu D untuk keluar, bot tetap jalan
```

### Cara C: Docker (paling rapi kalau sudah pakai Docker)

```bash
docker compose up -d
docker compose ps        # cek status
docker compose logs -f bot
```

`restart: unless-stopped` sudah ada di `docker-compose.yml`, jadi ini sudah
24 jam sejak awal. **Kalau kamu pakai Docker, sebenarnya ini sudah aman** —
masalahnya tadi karena bot dijalankan manual di terminal, bukan lewat compose.

---

## Sebab 2: pesanmu DIBUANG saat bot mati

Ini bagian yang paling menyakitkan, dan aku temukan saat memperbaiki.

Kode lama start dengan `drop_pending_updates=True`. Artinya: **semua pesan yang
kamu kirim waktu bot mati langsung dihapus**, sebelum sempat dibaca. Jadi kamu
kirim dari HP → bot mati → pesanmu hilang → nggak ada balesan, dan kamu tidak
tahu itu karena bug.

**Bukti nyata.** Saat memperbaiki ini, aku tanya ke server Telegram:

```
PENDING di server Telegram: 1 pesan menunggu
terbaca oleh getUpdates: 1
  id=547661328 text='/menu'
```

Ada **pesan sungguhan yang menunggu di antrean** — dan kode lama akan
menghapusnya. Sudah aku ubah jadi `drop_pending_updates=False`.

**Sekarang:** kirim pesan kapan saja, bot tetap membacanya saat hidup lagi.
Terlambat, tapi tidak hilang.

**Bonus:** bot sekarang kirim pesan `🟢 Bot hidup lagi` setiap kali nyala. Jadi
kamu tidak perlu menebak lagi — diam = mati, ada pesan = hidup.

---

## Soal "penuh" — jawabannya

Kamu tanya: *"yang penuh apanya?"*

Jawabannya: **bukan GitHub.** Aku cek repo-nya, cuma **3.2 MB, 85 file.**
Gambar dan video **tidak masuk git sama sekali** — sudah ada di `.gitignore`:

```
outputs/
data/
*.sqlite
```

Jadi GitHub kamu aman. Yang penuh kalau dibiarkan adalah **disk server**, bukan
GitHub.

### Yang benar-benar menghabiskan kuota

Kuota Cloudflare/host gratismu itu habis karena **teks chat**, bukan gambar.
Kamu bilang "24 persen baru beberapa chat" — itu wajar. Setiap chat mengirim
riwayat + konteks ke LLM, dan model besar mahal.

### Sekarang sudah ada yang jaga

`/disk` untuk lihat:

```
Penyimpanan: 512 MB dari 2048 MB (25%). Aku bersihkan otomatis kalau lewat batas.
```

Yang dihapus otomatis (tiap 6 jam):
- Gambar/video/audio lama, kalau lewat 2 GB atau lewat 14 hari

Yang **TIDAK PERNAH** dihapus:
- ✅ Login tersimpan (`data/profiles`) — kalau dihapus, kamu logout semua situs
- ✅ Memory database — kalau dihapus, bot lupa semuanya
- ✅ File 3 terbaru tiap jenis — kamu mungkin masih merujuk itu

Bersihkan manual: `/disk bersih`

Atur batasnya di `.env`:
```
OUTPUTS_MAX_MB=2048          # batas ukuran, MB
OUTPUTS_MAX_AGE_DAYS=14      # hapus yang lebih tua dari ini
RETENTION_INTERVAL_HOURS=6   # seberapa sering cek
```

---

## Soal "pindah ke cloud lain otomatis kalau penuh"

Aku jawab jujur: **tidak bisa otomatis saat sedang jalan.**

Kenapa? Karena bot itu pegang **koneksi Telegram** dan **sesi browser**. Kalau
kamu jalankan 2 server sekaligus, kamu akan dapat **balesan dobel** atau
**balesan tidak muncul sama sekali** — Telegram cuma izinkan satu bot mengambil
pesan.

**Tapi** — yang bisa dan sudah aku buat: **pindah cepat dan lengkap.**

```bash
./deploy/backup.sh
```

Hasilnya satu file kecil (tesnya 17 KB) berisi:
- ✅ Memory & semua yang bot ingat
- ✅ Login tersimpan ke semua situs
- ✅ Preferensi kamu

**Secret TIDAK masuk backup** — itu disengaja. File backup yang berisi key itu
kebocoran yang menunggu terjadi. Aku cek: **0 file `.env`/token** di dalamnya.

Cara pindah:
```bash
# di server lama
./deploy/backup.sh

# di server baru
tar -xzf backup-*.tar.gz      # mengembalikan data/
cp .env.example .env          # isi secret-nya lagi
docker compose up -d          # atau systemctl start ai-agent
```

Ada juga file `backup-*.NEEDED.txt` yang mengingatkan secret apa saja yang
harus kamu isi di server baru — supaya pindah tidak setengah jalan.

**Intinya:** pindah itu 2 menit, bukan otomatis. Menurutku itu pertukaran yang
benar: 2 menit kerjamu, daripada risiko balesan dobel selamanya.

---

## Kalau nanti nambah key dari provider lain

Kamu tanya: *"bisa pakai api key dari cloud lain?"* **Bisa**, dan sudah jalan.
Bot punya 7 provider dengan rotasi otomatis: kalau satu kena limit, langsung
pindah ke berikutnya.

Tambah key:
```
/keys add groq gsk_xxxxx
```
atau lewat situs:
```
ambil key di situsbaru.com
```

Cek stok: `/stok`

**Kalau semua provider kena limit**, bot bilang terus terang — tidak diam-diam
gagal.

---

## Checklist 24 jam

```
[ ] Bot dijalankan lewat systemd ATAU docker compose (bukan nohup)
[ ] systemctl enable ai-agent  (biar nyala saat reboot)
[ ] /status di Telegram -> harus menjawab
[ ] Kirim pesan saat bot mati -> harus dibaca saat hidup lagi
[ ] /disk -> cek batas penyimpanan
[ ] ./deploy/backup.sh -> pastikan backup jalan
```

Selesai. Setelah ini, kirim pesan kapan saja — dari HP, tengah malam, server
baru saja reboot — bot akan membacanya.
