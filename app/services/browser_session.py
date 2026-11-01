"""Persistent browser profiles, so a login survives between runs.

A browser that starts empty every time cannot do anything useful on a site that
needs an account: the agent would have to log in again on every message. This
module keeps one Chromium profile per user under ``data/browser_profiles/<id>``
(inside ``data/``, which is gitignored), so cookies and local storage persist.

That also makes the login step a one-time, user-driven action:

1. The user runs ``/login <site>`` and gets a real browser window (headful) with
   the site open. They sign in themselves - including any 2FA or captcha.
2. The profile is saved. Later ``/browse`` runs reuse it and start already
   authenticated, so the agent never handles a password.

Nothing here types credentials. The bot has no access to the user's password,
which is the whole point: a password that is never given cannot leak.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

# One permission decision per host, remembered so the user is asked once.
PERMISSION_FILE = "site_permissions.json"


def slugify_host(url_or_host: str) -> str:
    """'https://console.groq.com/keys' -> 'console.groq.com'."""
    text = (url_or_host or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[a-zA-Z]+://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.split("@")[-1]
    text = text.split(":")[0]
    return text.lower().strip()


def host_from_url(url: str) -> str:
    return slugify_host(url)


@dataclass
class Permission:
    host: str
    allowed: bool
    first_seen: float
    last_used: float


class SitePermissions:
    """Remembers which hosts the user allowed the agent to open.

    The user explicitly asked not to be re-asked for every site, while still
    wanting a gate before the agent wanders somewhere unfamiliar. One decision
    per host satisfies both: a new host prompts once, a known host does not.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, Permission] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("site permissions unreadable, starting fresh: %s", exc)
            return
        for host, entry in (raw or {}).items():
            if not isinstance(entry, dict):
                continue
            self._data[host] = Permission(
                host=host,
                allowed=bool(entry.get("allowed")),
                first_seen=float(entry.get("first_seen") or 0),
                last_used=float(entry.get("last_used") or 0),
            )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            host: {
                "allowed": p.allowed,
                "first_seen": p.first_seen,
                "last_used": p.last_used,
            }
            for host, p in self._data.items()
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_known(self, host: str) -> bool:
        return host in self._data

    def is_allowed(self, host: str) -> bool:
        entry = self._data.get(host)
        return bool(entry and entry.allowed)

    def remember(self, host: str, allowed: bool) -> None:
        now = time.time()
        existing = self._data.get(host)
        self._data[host] = Permission(
            host=host,
            allowed=allowed,
            first_seen=existing.first_seen if existing else now,
            last_used=now,
        )
        self._save()
        log.info("site permission %s -> %s", host, "allowed" if allowed else "denied")

    def touch(self, host: str) -> None:
        entry = self._data.get(host)
        if entry:
            entry.last_used = time.time()
            self._save()

    def forget(self, host: str) -> bool:
        if host in self._data:
            del self._data[host]
            self._save()
            return True
        return False

    def clear(self) -> int:
        count = len(self._data)
        self._data.clear()
        self._save()
        return count

    def allowed_hosts(self) -> list[str]:
        return sorted(h for h, p in self._data.items() if p.allowed)

    def denied_hosts(self) -> list[str]:
        return sorted(h for h, p in self._data.items() if not p.allowed)


class BrowserProfiles:
    """Per-user Chromium profiles under data/browser_profiles/."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.permissions = SitePermissions(self.root / PERMISSION_FILE)

    def profile_dir(self, user_id: int) -> Path:
        path = self.root / f"user_{int(user_id)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def has_profile(self, user_id: int) -> bool:
        path = self.root / f"user_{int(user_id)}"
        if not path.exists():
            return False
        # A profile with a cookie store is one the user has actually signed into.
        return any(path.iterdir())

    def wipe(self, user_id: int) -> bool:
        import shutil

        path = self.root / f"user_{int(user_id)}"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            log.info("wiped browser profile for user %s", user_id)
            return True
        return False


def build_profiles(settings) -> BrowserProfiles:
    root = Path(getattr(settings, "data_path", Path("data"))) / "browser_profiles"
    return BrowserProfiles(root)
