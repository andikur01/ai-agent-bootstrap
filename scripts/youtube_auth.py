#!/usr/bin/env python3
"""One-time manual YouTube OAuth.

Run this **yourself, on your own machine**, never through the bot:

    python scripts/youtube_auth.py

It opens a browser, you log in as yourself and approve the upload scope, and the
resulting token is written to ``YOUTUBE_TOKEN_FILE`` (default
``data/youtube_token.json``). The bot only ever reads that file; it never runs
this flow, never asks for your password, and never creates an account.

Requires ``YOUTUBE_CLIENT_SECRET_FILE`` pointing at a Google Cloud OAuth client
of type "Desktop app" that you created manually.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Butuh paket tambahan. Jalankan:\n  pip install google-auth-oauthlib")
        return 1

    from app.core.config import get_settings

    settings = get_settings()
    client_secret = Path(settings.youtube_client_secret_file or "")
    if not client_secret.exists():
        print(
            "YOUTUBE_CLIENT_SECRET_FILE belum diset atau filenya tidak ada.\n"
            "Buat OAuth client (tipe Desktop app) di Google Cloud Console, "
            "download JSON-nya, lalu isi path-nya di .env."
        )
        return 1

    token_path = Path(settings.youtube_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    print("Membuka browser untuk login. Login manual sebagai akun kamu sendiri.")
    credentials = flow.run_local_server(port=0)

    token_path.write_text(credentials.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    print(f"Token disimpan di {token_path} (permission 600).")
    print("Jangan commit file ini. Sudah masuk .gitignore.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
