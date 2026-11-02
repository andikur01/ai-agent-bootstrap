#!/usr/bin/env bash
# Move the bot to another server without losing its memory.
#
# The user's worry was concrete: "kalau penuh, bisa otomatis pindah ke cloud
# free yang lain?" The honest answer is that no host can be swapped at runtime -
# the bot holds a Telegram connection and a browser session, and a half-moved
# bot would answer twice or not at all. What does work is a fast, complete move:
# this writes one archive with the database, saved logins and preferences, and
# the other side restores it.
#
# Secrets are deliberately NOT in the archive. They are re-supplied on the new
# host from GitHub/HF secrets, because a backup file that travels with the keys
# in it is a leak waiting to happen.
set -eu

cd "$(dirname "$0")/.." || exit 1

OUT="${1:-backup-$(date +%Y%m%d-%H%M).tar.gz}"

if [ ! -d data ]; then
    echo "data/ tidak ada. Jalankan dari root project." >&2
    exit 1
fi

# The database, saved logins and preferences. Not outputs/ - those are generated
# files that can be re-made, and they are what fills the disk.
tar -czf "$OUT" \
    --exclude='data/profiles/*/Cache' \
    --exclude='data/profiles/*/Code Cache' \
    --exclude='data/profiles/*/GPUCache' \
    data/agent.sqlite3 \
    data/memory \
    data/profiles \
    2>/dev/null || true

# A reminder of what the new host still needs, so the move cannot silently
# half-finish.
cat > "${OUT%.tar.gz}.NEEDED.txt" <<'EOF'
Yang harus kamu siapkan di server baru (TIDAK ada di backup, sengaja):

  TELEGRAM_BOT_TOKEN
  TELEGRAM_ALLOWED_USER_IDS
  GROQ_API_KEY / GEMINI_API_KEY / ... (semua key provider)

Cara restore:
  tar -xzf <file>.tar.gz          # mengembalikan data/
  cp .env.example .env            # lalu isi secret-nya
  docker compose up -d            # atau: systemctl start ai-agent
EOF

size=$(du -h "$OUT" | cut -f1)
echo "Backup siap: $OUT ($size)"
echo "Isinya: memory, login tersimpan, preferensi. Secret TIDAK disertakan."
echo "Petunjuk server baru: ${OUT%.tar.gz}.NEEDED.txt"
