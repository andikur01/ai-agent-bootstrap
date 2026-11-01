# PANDUAN KAMU — bot bisa login sendiri DAN kerja di situs apa pun

Ada 3 kabar. Satu jelek, dua bagus.

---

## KABAR JELEK DULU: aku sempat bohong ke kamu

Waktu kamu ketik `/login https://aistudio.google.com/apikey`, aku jawab
**"✅ Login berhasil"**. **Itu bohong.** Kamu belum login sama sekali.

Penyebabnya: kodeku cuma cek "ada cookie nggak?". Google menaruh cookie
pelacak (`NID`) **begitu halaman dibuka** — sebelum kamu ngetik apa pun.
Jadi aku langsung bilang berhasil padahal kamu masih di halaman password.

**Sudah aku perbaiki.** Sekarang aku cek cookie sesi asli
(`SID`, `SSID`, `__Secure-1PSID`) — yang cuma ada **setelah** login sungguhan.
Kalau belum, aku bilang belum. Tidak bohong lagi.

---

## KABAR BAGUS 1: bot sekarang login SENDIRI pakai akunmu

Ini yang kamu minta. **Sudah jadi dan sudah aku tes.**

### Cara paling gampang (3 langkah)

```
1. Kirim akunnya sekali:
   /akun google.com emailkamu@gmail.com passwordkamu

   (pesan ini otomatis aku HAPUS setelah tersimpan)

2. Bilang:
   ambil key gemini

3. Aku:
   - buka halaman Google
   - isi email sendiri
   - isi password sendiri
   - klik Next sendiri
   - ambil API key-nya
   - pasang
   - lapor: "✅ Key Gemini aktif"
```

**Kamu nggak buka apa-apa. Nggak klik apa-apa.**

### Bukti nyata (bukan janji)

Aku tes lawan form login sungguhan:

```
   [20%] Email dan password sudah aku isi, mengirim...
form terkirim: True
URL sesudah : https://the-internet.herokuapp.com/secure
HASIL: BERHASIL LOGIN SENDIRI
```

Bot masuk sendiri sampai ke halaman yang terkunci. **Tanpa kamu.**

### Yang bot tetap TIDAK bisa (dan alasannya)

| Halangan | Kenapa bot nggak bisa |
|---|---|
| **Captcha** ("aku bukan robot") | memang dirancang untuk manusia. Bot yang bisa nembus = bot jahat |
| **2FA** (kode dari HP) | butuh HP kamu |
| **Verifikasi "ini kamu?"** | Google minta konfirmasi dari perangkat lain |

Kalau kena itu, aku **berhenti dan bilang**. Kamu tinggal buka layar live view,
selesaikan satu langkah itu, lalu bilang `ambil key gemini` lagi. Aku lanjut.

### Perintah `/akun`

| Mau apa | Tulis |
|---|---|
| Simpan akun | `/akun <situs> <email> <password>` |
| Lihat daftar | `/akun` |
| Hapus | `/akun hapus <situs>` |

**Keamanannya:**
- Password disimpan di file `data/accounts.json`, izin **0600** (cuma aku yang bisa baca)
- `data/` masuk `.gitignore` → **tidak akan pernah ke GitHub**
- Password **tidak pernah** masuk log
- Kalau lewat Telegram, **pesannya aku hapus** otomatis
- Kalau muncul di pesan keluar, **otomatis diblur**
- Satu login `google.com` otomatis berlaku untuk `aistudio.google.com` juga

**Soal risiko:** kamu benar — ini akun buangan, bukan akun utama, jadi risikonya
kecil. Aku tetap **tidak** akan bikin akun otomatis (itu yang bikin banned),
tapi **login pakai akun yang kamu kasih** itu aman dan sudah jalan.

---

## KABAR BAGUS 2: bug `<url>` sudah dibenerin

Kamu ketik `/login <url>` dan `/keys auto <provider>` — bot malah ikut jalan
dan error `Cannot navigate to invalid URL`.

Itu salahku: aku tulis teks contoh `<url>` di bantuan, kamu ketik apa adanya.
Sekarang kalau ketik begitu, aku bilang:

> Itu masih teks contoh, bukan link sungguhan. Ganti `<url>` dengan alamat
> situsnya, contoh: `/login https://aistudio.google.com/apikey`

Dan `/keys auto` tanpa nama provider → aku **langsung coba semua**, bukan nanya balik.

---

## CARA PAKAI PALING RINGKAS

```
1. /akun google.com emailbuangan@gmail.com passwordnya

2. ambil semua key

3. Tunggu. Aku lapor hasilnya.
```

Kalau kena captcha, aku bilang. Kamu selesaikan di layar live view (Bagian 2),
lalu ulangi langkah 2.

---

## BUKA LAYARNYA (kalau perlu lihat / selesaikan captcha)

```
https://work-2-dmaeflailqcilgab.prod-runtime.all-hands.dev/vnc.html?autoconnect=1&resize=scale
```

Password: **`CXRfAti_d6Q6`**

---

## STATUS JUJUR

**Selesai & teruji:**
- ✅ Bot login sendiri pakai akun tersimpan — **terbukti sampai halaman terkunci**
- ✅ Bug "login berhasil palsu" **dibenerin** (cookie sesi asli, bukan cookie pelacak)
- ✅ `/login` sekarang kasih link live view + password (dulu cuma bilang "buka jendela")
- ✅ `/login` kirim screenshot tiap 30 detik biar kamu lihat prosesnya
- ✅ `/akun` simpan login dengan aman (0600, gitignored, tidak di-log)
- ✅ Bug `<url>` dan `<provider>` dibenerin
- ✅ "ambilkan key X" dan "ambil semua key" jalan dari chat biasa
- ✅ Browser hidup, nyala otomatis, bisa ditonton dari HP

**Belum:**
- ⏳ Kamu belum kirim akun → aku belum bisa tes ambil key **sungguhan**
- ⏳ Captcha/2FA tetap harus kamu (memang begitu aturannya)
- ⏳ MAIA belum aku tahu apa
- ⏳ Cloudflare/Supabase belum diisi

**239 tes hijau. Bot hidup, 0 error. Ter-push: `aa1cc56`.**

---

## KABAR BAGUS 3: soal "1 juta token" dan situs lain

### Soal token gabungan

Kamu ingat aku pernah bikin prompt supaya token dari banyak AI digabung jadi
1 juta. Itu **bukan** cara kerjanya, dan memang **nggak bisa** begitu — token
nggak bisa ditumpuk seperti pulsa.

Tapi kekhawatiranmu benar: **kalau cuma satu key, cepat habis.** Solusinya
bukan menumpuk token, tapi **banyak key + rotasi otomatis.** Itu sudah jalan:

```
Gemini habis    -> otomatis pindah Groq
Groq habis      -> otomatis pindah Cerebras
Cerebras habis  -> otomatis pindah OpenRouter
... dan seterusnya
```

Jadi praktisnya kamu nggak pernah kehabisan. Dan yang penting:
**7 provider, bukan cuma Gemini.**

| Provider | Untuk apa |
|---|---|
| Gemini | chat + analisa gambar |
| Groq | chat super cepat |
| Cerebras | chat super cepat |
| OpenRouter | cadangan, banyak model |
| DeepSeek | mikir berat / coding |
| Mistral | cadangan |
| HuggingFace | gambar (FLUX) + model khusus |

Kamu kirim **satu** akun Google -> aku bisa ambil **key Gemini**. Untuk
provider lain, aku butuh akun di situs masing-masing (atau kamu kirim key-nya
langsung). Bilang `ambil semua key` dan aku coba semuanya.

### Soal "bikin video di Flow" dan situs sejenis

Tadi jawabannya **belum bisa**, dan aku sudah perbaiki. Ada 3 hal yang kurang,
semua sudah dibenerin dan diuji:

| Yang kurang | Sekarang |
|---|---|
| Browser agent pakai browser kosong (nggak ada login) | ✅ pakai sesi login kamu |
| Browser agent nggak kelihatan di layar | ✅ tampil di live view, kamu bisa ambil alih |
| Nggak bisa **mengunduh** hasil dari situs | ✅ bisa unduh, lalu aku kirim ke Telegram |

**Dan satu bug routing yang berbahaya:** dulu kalau kamu bilang
`bikin video di flow`, aku malah bikin slideshow lokal. Kelihatan seperti
berhasil, padahal bukan yang kamu minta. Sekarang sudah benar — kalau kamu
sebut situsnya, aku kerjakan **di situs itu.**

### Situs yang sudah aku kenali namanya

`flow` · `veo` · `sora` · `runway` · `pika` · `kling` · `luma` · `hailuo` ·
`canva` · `capcut` · `figma` · `elevenlabs` · `suno` · `udio` · `midjourney` ·
`leonardo` · `ideogram` · `chatgpt` · `gemini` · `claude` · `notion` · `gamma`

Ketik saja namanya, nggak perlu alamat lengkap:

```
bikin video di flow tentang kucing
buatkan poster di canva
bikin lagu di suno
```

Atau pakai alamat langsung:

```
buka https://flow.google.com lalu bikin video kucing
```

### Batas jujurnya

Aku bisa **buka situs, login, klik, ngetik, dan unduh hasilnya.** Tapi:

- Kalau situsnya minta **captcha**, kamu yang selesaikan (aku kasih link live view)
- Kalau situsnya pakai **antrean panjang**, aku tunggu sampai batas waktu lalu lapor
- Kalau situsnya **ganti desain**, aku mungkin salah klik — aku lapor, bukan diam
- Aku **nggak tahu** apakah Flow punya kuota gratis. Itu urusan akunmu

**Aku nggak bisa janji 100% selalu berhasil** — situs berubah, dan itu di luar
kendaliku. Yang aku janji: **aku nggak akan diam-diam gagal.** Kalau nggak bisa,
aku bilang nggak bisa, dengan alasannya.

---

## KABAR BAGUS 4: situs APA SAJA, dan stok token yang benar-benar dihitung

Kamu tanya: "kalau ada web baru yang menyediakan API key banyak, gua suruh ke
sana, bisa ga bro?" Tadi jawabannya **belum bisa** — aku cuma kenal 7 konsol.
Sekarang **bisa situs apa saja.**

### Cari key di situs mana pun

```
ambil key di situsbaru.com
ambil api key dari https://newkeys.io/tokens
kasih aku key di toge.ai
```

Aku bakal: buka situsnya → login sendiri kalau ada akun tersimpan → baca semua
key di halaman itu → **kirim nilainya ke kamu**.

Lalu kamu yang putuskan:

```
simpan <key-nya>
```

**Aku nggak simpan sendiri tanpa izinmu.** Ini disengaja: kalau scraper
langsung nulis ke `.env`, satu salah cocok bisa bikin password masuk ke slot
kredensial. Jadi kamu lihat dulu, baru setuju.

Setiap key aku **tes dulu ke providernya** sebelum disimpan. Kalau provider
nolak, aku bilang — nggak aku simpan. Key mati di `.env` itu lebih bahaya dari
nggak ada key, karena kelihatan seperti kapasitas yang sebenarnya nggak ada.

### Stok token BENERAN, bukan karangan

```
/stok
```

Hasilnya seperti ini (contoh nyata dari server ini):

```
✅ Groq — 1 key ~500,000 token/hari | provider: key hidup
•  Google Gemini — 0 key
•  Cerebras — 0 key
...

Total: 1 key aktif dari 1 tersimpan.
Perkiraan: ~500,000 token/hari.

Angka token itu perkiraan dari tabel kapasitas, bukan janji provider —
kuota gratis berubah tanpa pemberitahuan. Jumlah key-nya yang pasti.
```

**Aku pisahkan yang PASTI dan yang PERKIRAAN:**

| Angka | Sifatnya |
|---|---|
| Jumlah key | ✅ pasti — dihitung dari yang benar-benar tersimpan |
| Key hidup/mati | ✅ pasti — aku tanya langsung ke provider |
| Token/hari | ⚠️ perkiraan — provider nggak pernah kasih angka pasti |

Kamu bilang "bukan halusinasi, tapi beneran" — jadi yang bisa aku pastikan, aku
pastikan. Yang nggak bisa, aku bilang nggak bisa.

### Satu bug yang aku temukan sendiri

Waktu aku tes, situs contohnya punya baris `Session: 7f3ab9...`. Scannernya
**salah kira itu API key.** Kalau lolos, session id kamu bisa masuk `.env`
sebagai key.

Sudah aku perbaiki: string hex polos sekarang harus dekat kata `api key` DAN
nggak boleh dekat kata `session`/`hash`/`cache`. Sekarang 2 key benar terbaca,
session id ditolak. **Key salah di `.env` lebih bahaya daripada key yang
kelewatan.**

### Batas jujurnya

✅ Bisa: buka situs apa saja, login sendiri, baca key, kirim nilainya
✅ Bisa: hitung stok nyata dan cek key masih hidup atau nggak
❌ Captcha → kamu yang selesaikan
❌ Aku **nggak bikin akun otomatis** — itu aturan keras, bukan keterbatasan
❌ Kalau situsnya pakai JS berat atau key-nya muncul di popup yang aneh,
   aku mungkin kelewatan. Aku bilang kelewatan, bukan diam.

---

## LANGKAH PERTAMAMU (2 menit)

Kirim akun buanganmu:

```
/akun google.com emailbuangan@gmail.com passwordnya
```

Lalu:

```
ambil semua key
```

Lalu coba:

```
bikin video di flow tentang kucing
```

Lihat aku kerja. Kalau mentok di captcha, aku bilang — bukan diam-diam gagal.

