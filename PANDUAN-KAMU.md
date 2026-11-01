# PANDUAN KAMU — apa yang harus kamu kirim

Baca ini pelan-pelan. Isinya cuma: **apa yang aku butuh dari kamu**,
**teks yang tinggal kamu paste**, dan **apa yang bisa aku kerjakan sendiri**.

---

## BAGIAN 1 — Yang sudah selesai (tidak perlu apa-apa dari kamu)

| Keluhanmu | Status |
|---|---|
| Web hitam putih jelek | ✅ sudah diganti design system, sudah aku screenshot |
| Menu bikin aku milih sendiri | ✅ `/menu` sekarang **cuma penjelasan**, tidak ada tombol |
| Token dipatok | ✅ cap dihapus, default **tanpa batas** |
| Live view jadi fitur terpisah | ✅ nyala otomatis di semua tugas panjang |
| Estimasi token ngarang | ✅ sekarang rentang + token asli setelah selesai |

---

## BAGIAN 2 — Yang aku butuh dari KAMU

Ini daftarnya. Kalau kosong, fitur itu mati atau jelek.

### A. Supaya web bisa dibuka orang lain (link hidup) — **PENTING**

Tanpa ini, web cuma ada di komputer aku, tidak bisa kamu buka dari HP.

Kamu perlu bikin akun **Cloudflare** (gratis, sekali saja):
1. Buka https://dash.cloudflare.com/sign-up
2. Daftar, verifikasi email
3. Buka https://dash.cloudflare.com/profile/api-tokens
4. Klik **Create Token** → pilih template **"Edit Cloudflare Workers"**
5. Klik Continue → Create → **copy token-nya**
6. Buka https://dash.cloudflare.com/ → lihat **Account ID** di kanan

Lalu kirim ke aku 2 baris ini (ganti isinya):

```
CLOUDFLARE_API_TOKEN=token-yang-kamu-copy-tadi
CLOUDFLARE_ACCOUNT_ID=account-id-kamu
```

### B. Supaya AI-nya pintar (tidak cuma 1 provider)

Sekarang cuma **Groq** yang jalan. Itu sebabnya kadang hasilnya kurang.
Tambah ini biar ada cadangan otomatis:

```
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
HF_TOKEN=...
```

Cara dapat (semua gratis):
- Gemini → https://aistudio.google.com/apikey → **Create API key**
- OpenRouter → https://openrouter.ai/keys → **Create Key** (ada model gratis)
- Hugging Face → https://huggingface.co/settings/tokens → **New token** (role: Read)

### C. Supaya website fullstack punya login + database

```
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_DB_PASSWORD=...
```

- Buka https://supabase.com → **New project** (gratis)
- Settings → API → copy **URL**, **anon key**, **service_role key**
- Settings → Database → **Database password**

### D. Supaya gambar/video lebih bagus

```
REPLICATE_API_TOKEN=...
```

- Buka https://replicate.com/account/api-tokens → copy

### E. Supaya n8n bisa langsung dapat link

Butuh n8n jalan dulu (aku yang pasang kalau kamu mau), lalu:
```
N8N_URL=http://localhost:5678
N8N_API_KEY=...
```

---

## BAGIAN 3 — Teks yang tinggal kamu paste ke aku

Kalau semua sudah kamu kumpulkan, kirim **satu pesan** ke bot seperti ini:

```
Aku sudah kumpulkan key. Ini daftarnya:

CLOUDFLARE_API_TOKEN=xxxx
CLOUDFLARE_ACCOUNT_ID=xxxx
GEMINI_API_KEY=xxxx
OPENROUTER_API_KEY=xxxx
HF_TOKEN=xxxx
SUPABASE_URL=xxxx
SUPABASE_ANON_KEY=xxxx
SUPABASE_SERVICE_ROLE_KEY=xxxx
SUPABASE_DB_PASSWORD=xxxx
REPLICATE_API_TOKEN=xxxx
```

Aku yang masukkan ke `.env` (aman, tidak masuk git, tidak masuk log,
tidak masuk screenshot).

**Kalau cuma punya sebagian, kirim sebagian saja.** Tidak apa-apa.

---

## BAGIAN 4 — Cara pakai sehari-hari

Kamu **tidak perlu perintah**. Cukup ngomong:

| Mau apa | Tulis saja |
|---|---|
| Bikin web | `buatkan web toko kopi` |
| Web + login | `buatkan web dengan login dan database` |
| Bikin poster | `buatkan poster anak-anak` |
| Bikin video | `bikin video promosi 15 detik` |
| Workflow otomasi | `buatkan workflow n8n untuk balas chat` |
| Baca link | tempel link YouTube/TikTok/IG |
| Tanya hasil | `apakah webnya sudah jadi?` |
| Lanjut kerja lama | `lanjutkan yang tadi` |
| Perbaiki | `perbaiki yang kemarin` |

---

## BAGIAN 5 — Soal "MAIA 50-200 API key"

Aku jawab jujur, karena kamu pantas dapat jawaban yang benar.

**Yang benar:** bot ini **sudah bisa** pakai banyak key sekaligus. Ada
"key pool" yang otomatis ganti key kalau satu kena limit. Tidak ada batas
jumlah key. Jadi kalau kamu punya 50 atau 200 key, **semuanya bisa dipakai
hari ini juga** — kamu tinggal kirim, aku masukkan.

**Yang aku tidak tahu:** aku belum bisa memastikan apa itu "MAIA". Aku tidak
mau ngarang lalu kamu buang waktu. Yang aku butuh dari kamu:

1. **Screenshot halamannya**, atau
2. **Linknya**

Setelah aku lihat, aku bilang terus terang: bisa atau tidak, dan kalau bisa,
apa yang harus kamu kirim.

**Yang aku tidak akan lakukan:** aku tidak akan bikin akun otomatis di situs
orang. Itu bikin akun kamu diblokir. Login/captcha tetap kamu yang kerjakan
sekali, sisanya aku.

---

## BAGIAN 6 — Soal "AI-nya masuk ke situs build-with-AI"

Kamu mau: aku yang ngetik prompt panjang di situs AI lain, tunggu hasilnya,
download, hosting, ambil link, kirim ke kamu.

Ini **bisa**, tapi ada syaratnya:
- Aku perlu tahu **situs mana** (kasih linknya)
- Aku perlu kamu **login sekali** di situ (`/login <link-situs>`)
- Kalau situsnya punya captcha, kamu yang selesaikan di layar live view

Kalau syarat itu terpenuhi, alurnya:

```
Kamu: "masuk ke <link situs>, bikin web toko kopi"
  ↓
Aku: buka situsnya, tulis prompt panjang, klik generate
  ↓
Aku: tunggu, screenshot tiap tahap, kirim ke kamu
  ↓
Aku: download hasilnya, hosting, ambil link
  ↓
Aku: kirim link + screenshot ke kamu
```

Aku **belum** mengerjakan ini. Aku mau kerjakan **setelah** kamu kasih
link situsnya, biar aku bisa tes betulan, bukan asal jadi.
