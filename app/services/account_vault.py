"""Saved logins the agent uses to sign in on the user's behalf.

The user asked for this directly and owns the accounts involved: they create a
throwaway email, hand it over, and the agent signs in and fetches API keys. The
alternative - the user opening a console and copying keys by hand - is the chore
this whole feature exists to remove.

Storage rules, in order of importance:

1. Never in git. The file lives under ``data/``, which is gitignored.
2. Never in a log. Nothing here is ever logged, and ``redact()`` catches the
   values if they reach an outbound message.
3. Never world-readable. Written 0600, in a directory created 0700.
4. Never echoed back. ``describe()`` returns the email and a masked password, so
   a status message can confirm what is stored without repeating the secret.

The password does travel through Telegram once, when the user sends it. The bot
deletes that message after saving, so it does not sit in the chat history.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Account:
    host: str
    email: str
    password: str
    note: str = ""

    def masked_password(self) -> str:
        if len(self.password) <= 2:
            return "*" * len(self.password)
        return self.password[0] + "*" * (len(self.password) - 2) + self.password[-1]

    def describe(self) -> str:
        return f"{self.email} (password {self.masked_password()})"


class AccountVault:
    """Reads and writes saved logins for one deployment."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._accounts: dict[str, Account] = {}
        self._load()

    # ------------------------------------------------------------------ loading
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("could not read the account vault: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        for host, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            email = str(entry.get("email") or "").strip()
            password = str(entry.get("password") or "")
            if not email or not password:
                continue
            self._accounts[host.lower()] = Account(
                host=host.lower(),
                email=email,
                password=password,
                note=str(entry.get("note") or ""),
            )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        payload = {
            host: {"email": acc.email, "password": acc.password, "note": acc.note}
            for host, acc in self._accounts.items()
        }
        # Write through a temp file so a crash cannot leave a truncated vault.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ------------------------------------------------------------------- public
    def get(self, host: str) -> Account | None:
        """Return the account for *host*, matching parent domains too.

        ``aistudio.google.com`` should find an account saved for ``google.com``,
        because that is how the user thinks about it: one Google login.
        """
        host = (host or "").lower().lstrip(".")
        if not host:
            return None
        if host in self._accounts:
            return self._accounts[host]
        parts = host.split(".")
        for index in range(1, len(parts) - 1):
            parent = ".".join(parts[index:])
            if parent in self._accounts:
                return self._accounts[parent]
        return None

    def put(self, host: str, email: str, password: str, note: str = "") -> Account:
        account = Account(
            host=host.lower().lstrip("."), email=email.strip(), password=password, note=note
        )
        self._accounts[account.host] = account
        self._save()
        log.info("saved a login for %s (email not logged)", account.host)
        return account

    def forget(self, host: str) -> bool:
        removed = self._accounts.pop((host or "").lower().lstrip("."), None)
        if removed is not None:
            self._save()
        return removed is not None

    def clear(self) -> int:
        count = len(self._accounts)
        self._accounts.clear()
        self._save()
        return count

    def hosts(self) -> list[str]:
        return sorted(self._accounts)

    def describe(self) -> list[str]:
        return [f"{a.host} - {a.describe()}" for a in self._accounts.values()]


def build_vault(settings) -> AccountVault:
    data_dir = getattr(settings, "data_dir", "") or os.environ.get("DATA_DIR", "data")
    return AccountVault(Path(data_dir) / "accounts.json")
