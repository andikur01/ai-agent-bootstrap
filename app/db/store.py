"""SQLite persistence layer.

One connection guarded by a lock, accessed through ``run_blocking`` from async
code. SQLite is enough here: single-user personal bot, low write volume, and it
keeps the deploy footprint small (no extra service to run).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.utils import run_blocking

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    prefs       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_user_kind ON artifacts(user_id, kind, id DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    result      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, id DESC);

CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    run_at      TEXT NOT NULL,
    text        TEXT NOT NULL,
    repeat      TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_active ON schedules(active, run_at);

CREATE TABLE IF NOT EXISTS usage (
    user_id     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_hits       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, model)
);

CREATE TABLE IF NOT EXISTS learnings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    topic       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    sources     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learnings_user ON learnings(user_id, id DESC);

CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL DEFAULT 0,
    module      TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------------- low level ----------------
    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- users ----------------
    async def upsert_user(self, user_id: int, username: str = "", first_name: str = "") -> dict:
        now = _now()
        await run_blocking(
            self._exec,
            """
            INSERT INTO users (user_id, username, first_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                updated_at=excluded.updated_at
            """,
            (user_id, username, first_name, now, now),
        )
        return await self.get_user(user_id) or {}

    async def get_user(self, user_id: int) -> dict | None:
        return await run_blocking(
            self._query_one, "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )

    async def set_pref(self, user_id: int, key: str, value: Any) -> None:
        user = await self.get_user(user_id) or {"prefs": "{}"}
        prefs = json.loads(user.get("prefs") or "{}")
        prefs[key] = value
        await run_blocking(
            self._exec,
            "UPDATE users SET prefs = ?, updated_at = ? WHERE user_id = ?",
            (json.dumps(prefs, ensure_ascii=False), _now(), user_id),
        )

    async def get_prefs(self, user_id: int) -> dict:
        user = await self.get_user(user_id)
        if not user:
            return {}
        return json.loads(user.get("prefs") or "{}")

    # ---------------- messages ----------------
    async def add_message(
        self, user_id: int, role: str, content: str, meta: dict | None = None
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages (user_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
                (user_id, role, content, json.dumps(meta or {}), _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    async def recent_messages(self, user_id: int, limit: int = 20) -> list[dict]:
        rows = await run_blocking(
            self._query,
            "SELECT * FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return list(reversed(rows))

    async def search_messages(self, user_id: int, keyword: str, limit: int = 10) -> list[dict]:
        return await run_blocking(
            self._query,
            """
            SELECT * FROM messages
            WHERE user_id = ? AND content LIKE ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, f"%{keyword}%", limit),
        )

    async def count_messages(self, user_id: int) -> int:
        row = await run_blocking(
            self._query_one,
            "SELECT COUNT(*) AS c FROM messages WHERE user_id = ?",
            (user_id,),
        )
        return int(row["c"]) if row else 0

    # ---------------- artifacts ----------------
    async def add_artifact(
        self, user_id: int, kind: str, title: str, location: str, meta: dict | None = None
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO artifacts (user_id, kind, title, location, meta, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (user_id, kind, title, location, json.dumps(meta or {}), _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    async def latest_artifact(self, user_id: int, kind: str | None = None) -> dict | None:
        if kind:
            return await run_blocking(
                self._query_one,
                """
                SELECT * FROM artifacts WHERE user_id = ? AND kind = ?
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, kind),
            )
        return await run_blocking(
            self._query_one,
            "SELECT * FROM artifacts WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    async def list_artifacts(self, user_id: int, limit: int = 10) -> list[dict]:
        return await run_blocking(
            self._query,
            "SELECT * FROM artifacts WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    async def search_artifacts(self, user_id: int, keyword: str, limit: int = 5) -> list[dict]:
        return await run_blocking(
            self._query,
            """
            SELECT * FROM artifacts
            WHERE user_id = ? AND (title LIKE ? OR location LIKE ? OR meta LIKE ?)
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
        )

    # ---------------- tasks ----------------
    async def create_task(self, user_id: int, kind: str, payload: dict | None = None) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tasks (user_id, kind, status, payload, created_at, updated_at)
                VALUES (?,?,?,?,?,?)
                """,
                (user_id, kind, "pending", json.dumps(payload or {}), now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    async def update_task(self, task_id: int, status: str, result: str = "") -> None:
        await run_blocking(
            self._exec,
            "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            (status, result, _now(), task_id),
        )

    async def count_tasks_today(self, user_id: int, kind: str | None = None) -> int:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        if kind:
            row = await run_blocking(
                self._query_one,
                "SELECT COUNT(*) AS c FROM tasks WHERE user_id = ? AND kind = ? AND created_at >= ?",
                (user_id, kind, since.isoformat()),
            )
        else:
            row = await run_blocking(
                self._query_one,
                "SELECT COUNT(*) AS c FROM tasks WHERE user_id = ? AND created_at >= ?",
                (user_id, since.isoformat()),
            )
        return int(row["c"]) if row else 0

    # ---------------- schedules ----------------
    async def add_schedule(
        self, user_id: int, chat_id: int, run_at: str, text: str, repeat: str = ""
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO schedules (user_id, chat_id, run_at, text, repeat, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (user_id, chat_id, run_at, text, repeat, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    async def active_schedules(self) -> list[dict]:
        return await run_blocking(
            self._query,
            "SELECT * FROM schedules WHERE active = 1 ORDER BY run_at ASC",
        )

    async def deactivate_schedule(self, schedule_id: int) -> None:
        await run_blocking(
            self._exec, "UPDATE schedules SET active = 0 WHERE id = ?", (schedule_id,)
        )

    async def list_schedules(self, user_id: int) -> list[dict]:
        return await run_blocking(
            self._query,
            "SELECT * FROM schedules WHERE user_id = ? AND active = 1 ORDER BY run_at ASC",
            (user_id,),
        )

    # ---------------- usage ----------------
    async def add_usage(
        self,
        user_id: int,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_hits: int = 0,
    ) -> None:
        await run_blocking(
            self._exec,
            """
            INSERT INTO usage (user_id, day, model, prompt_tokens, completion_tokens, cached_hits)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id, day, model) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                cached_hits = cached_hits + excluded.cached_hits
            """,
            (user_id, _today(), model, prompt_tokens, completion_tokens, cached_hits),
        )

    async def usage_today(self, user_id: int) -> dict:
        rows = await run_blocking(
            self._query,
            "SELECT * FROM usage WHERE user_id = ? AND day = ?",
            (user_id, _today()),
        )
        prompt = sum(int(r["prompt_tokens"]) for r in rows)
        completion = sum(int(r["completion_tokens"]) for r in rows)
        cached = sum(int(r["cached_hits"]) for r in rows)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cached_hits": cached,
            "per_model": rows,
        }

    # ---------------- learnings ----------------
    async def add_learning(
        self, user_id: int, topic: str, summary: str, sources: list[str] | None = None
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO learnings (user_id, topic, summary, sources, created_at)
                VALUES (?,?,?,?,?)
                """,
                (user_id, topic, summary, json.dumps(sources or []), _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    async def list_learnings(self, user_id: int, limit: int = 20) -> list[dict]:
        return await run_blocking(
            self._query,
            "SELECT * FROM learnings WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    # ---------------- errors & feedback ----------------
    async def log_error(self, module: str, message: str, user_id: int = 0) -> None:
        await run_blocking(
            self._exec,
            "INSERT INTO errors (user_id, module, message, created_at) VALUES (?,?,?,?)",
            (user_id, module, message[:2000], _now()),
        )

    async def recent_errors(self, limit: int = 20) -> list[dict]:
        return await run_blocking(
            self._query, "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)
        )

    async def add_feedback(self, user_id: int, text: str) -> None:
        await run_blocking(
            self._exec,
            "INSERT INTO feedback (user_id, text, created_at) VALUES (?,?,?)",
            (user_id, text[:2000], _now()),
        )
