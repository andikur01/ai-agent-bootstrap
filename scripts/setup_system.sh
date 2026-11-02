#!/usr/bin/env bash
# Install the pieces the agent needs from the OS, then check them.
#
# Every one of these is a hard dependency of a feature the user asked for:
# live view needs all of xvfb, x11vnc, novnc and websockify; the browser needs
# Chromium; media work needs ffmpeg. They were missing on the first deploy and
# the features reported themselves unavailable, which looked like a code bug.
#
# Safe to run more than once.
set -uo pipefail

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/3  Paket sistem (butuh sudo)"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        ffmpeg xvfb x11vnc novnc websockify x11-apps scrot \
        fonts-dejavu-core fonts-noto-color-emoji \
        curl ca-certificates
    echo "selesai"
else
    echo "apt-get tidak ada. Lewati; install manual:"
    echo "  ffmpeg xvfb x11vnc novnc websockify x11-apps scrot"
fi

say "2/3  Browser Chromium untuk Playwright"
if [ -x .venv/bin/python ]; then
    .venv/bin/python -m playwright install --with-deps chromium || \
        .venv/bin/python -m playwright install chromium
else
    python3 -m playwright install chromium
fi

say "3/3  Cek semuanya"
missing=0
for tool in ffmpeg Xvfb x11vnc websockify xwd scrot; do
    if command -v "$tool" >/dev/null 2>&1; then
        printf '  ok    %s\n' "$tool"
    else
        printf '  HILANG %s\n' "$tool"
        missing=1
    fi
done
if [ -d /usr/share/novnc ]; then
    printf '  ok    noVNC (%s)\n' /usr/share/novnc
else
    printf '  HILANG noVNC (/usr/share/novnc)\n'
    missing=1
fi

if [ "$missing" -eq 0 ]; then
    say "Semua siap. Live view, browser dan ffmpeg bisa dipakai."
else
    say "Ada yang belum lengkap - lihat daftar HILANG di atas."
    exit 1
fi
