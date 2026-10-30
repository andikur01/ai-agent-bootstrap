"""Scheduler: reminders and recurring jobs, persisted in SQLite.

APScheduler runs inside the bot process. Jobs are stored in our own ``schedules``
table (not APScheduler's jobstore) so the source of truth is one place and the
user can list/cancel them with plain SQL-backed commands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.errors import ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

DURATION_RE = re.compile(
    r"(?:dalam|in)?\s*(\d+)\s*(detik|menit|jam|hari|sec|second|min|minute|hour|day)s?\b", re.I
)
CLOCK_RE = re.compile(r"(?:jam|pukul|at)\s*(\d{1,2})(?:[.:](\d{2}))?\b", re.I)

# words that carry scheduling meaning but are not part of the reminder body
NOISE_WORDS = re.compile(
    r"\b(ingatkan|ingetin|reminder|tolong|aku|saya|untuk|buat|jadwalkan|jadwal|"
    r"besok|kemarin|lusa|lagi|nanti|ya|dong|please|remind|me)\b",
    re.I,
)

UNIT_SECONDS = {
    "detik": 1,
    "sec": 1,
    "second": 1,
    "menit": 60,
    "min": 60,
    "minute": 60,
    "jam": 3600,
    "hour": 3600,
    "hari": 86400,
    "day": 86400,
}

REPEAT_WORDS = {
    "setiap hari": "daily",
    "tiap hari": "daily",
    "harian": "daily",
    "daily": "daily",
    "setiap minggu": "weekly",
    "tiap minggu": "weekly",
    "mingguan": "weekly",
    "weekly": "weekly",
}


@dataclass
class ParsedSchedule:
    when: datetime
    repeat: str
    text: str
    human: str


def parse_reminder(text: str, *, tz: str = "Asia/Jakarta", now: datetime | None = None) -> ParsedSchedule:
    """Parse Indonesian reminder phrasing into an absolute time.

    Understands "dalam 10 menit", "jam 9", "besok jam 7 pagi", "setiap hari jam 8".
    Raises ``ToolError`` when no time reference is present, so the bot can ask
    the user instead of silently scheduling something wrong.
    """
    zone = ZoneInfo(tz)
    now = now or datetime.now(zone)
    lowered = text.lower()

    repeat = ""
    for phrase, value in REPEAT_WORDS.items():
        if phrase in lowered:
            repeat = value
            break

    message = _extract_message(text)

    # relative: "dalam 10 menit"
    duration_match = DURATION_RE.search(lowered)
    if duration_match and not CLOCK_RE.search(lowered):
        amount = int(duration_match.group(1))
        unit = duration_match.group(2).lower()
        delta = timedelta(seconds=amount * UNIT_SECONDS.get(unit, 60))
        when = now + delta
        return ParsedSchedule(when, repeat, message, f"{amount} {unit} lagi")

    # absolute clock: "jam 9", "besok jam 7 pagi"
    clock = CLOCK_RE.search(lowered)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        if any(word in lowered for word in ("malam", "sore", "petang")) and hour < 12:
            hour += 12
        elif "siang" in lowered and hour < 12:
            hour = hour if hour == 12 else hour + 12
        hour = max(0, min(23, hour))

        day_offset = 0
        if "besok" in lowered or "tomorrow" in lowered:
            day_offset = 1
        elif "lusa" in lowered:
            day_offset = 2

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        target += timedelta(days=day_offset)
        if target <= now and not repeat:
            target += timedelta(days=1)
        human = target.strftime("%d %b %H:%M")
        if repeat:
            human = f"setiap {repeat.replace('daily', 'hari').replace('weekly', 'minggu')} {target.strftime('%H:%M')}"
        return ParsedSchedule(target, repeat, message, human)

    raise ToolError(
        "Aku belum paham waktunya. Contoh: \"ingatkan 10 menit lagi minum air\" "
        "atau \"besok jam 7 pagi olahraga\"."
    )


def _extract_message(text: str) -> str:
    """Strip the scheduling clause, keep the actual reminder body.

    Order matters: durations and clock references are removed *before* generic
    noise words, otherwise "menit" gets eaten by the "min" alternative and
    "minum" survives as a fragment.
    """
    cleaned = DURATION_RE.sub(" ", text)
    cleaned = CLOCK_RE.sub(" ", cleaned)
    for phrase in REPEAT_WORDS:
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(setiap|tiap|pagi|siang|sore|malam|petang)\b", " ", cleaned, flags=re.I)
    cleaned = NOISE_WORDS.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned or "Pengingat"


class SchedulerService:
    def __init__(self, store, settings, notifier=None):
        self.store = store
        self.settings = settings
        self.notifier = notifier
        self._scheduler = None
        self._job_ids: dict[int, str] = {}

    async def start(self) -> bool:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError:
            log.warning("APScheduler tidak terpasang; scheduler dimatikan")
            return False

        self._scheduler = AsyncIOScheduler(timezone=self.settings.tz)
        self._scheduler.start()
        for row in await self.store.active_schedules():
            self._register_row(row)
        log.info("scheduler started with %s jobs", len(self._job_ids))
        return True

    async def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as exc:  # noqa: BLE001
                log.debug("scheduler shutdown: %s", exc)
            self._scheduler = None

    # ------------------------------------------------------------------ api
    async def schedule(
        self,
        *,
        user_id: int,
        chat_id: int,
        text: str,
        when: datetime,
        repeat: str = "",
    ) -> dict:
        schedule_id = await self.store.add_schedule(
            user_id, chat_id, when.astimezone(timezone.utc).isoformat(), text, repeat
        )
        row = {
            "id": schedule_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "run_at": when.astimezone(timezone.utc).isoformat(),
            "text": text,
            "repeat": repeat,
            "active": 1,
        }
        self._register_row(row)
        return row

    async def cancel(self, user_id: int, schedule_id: int) -> bool:
        rows = await self.store.list_schedules(user_id)
        if not any(int(row["id"]) == schedule_id for row in rows):
            return False
        await self.store.deactivate_schedule(schedule_id)
        job_id = self._job_ids.pop(schedule_id, None)
        if job_id and self._scheduler:
            try:
                self._scheduler.remove_job(job_id)
            except Exception as exc:  # noqa: BLE001
                log.debug("remove job: %s", exc)
        return True

    async def list_for(self, user_id: int) -> list[dict]:
        return await self.store.list_schedules(user_id)

    def _register_row(self, row: dict) -> None:
        if self._scheduler is None:
            return
        schedule_id = int(row["id"])
        run_at = datetime.fromisoformat(row["run_at"])
        zone = ZoneInfo(self.settings.tz)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        local = run_at.astimezone(zone)
        repeat = row.get("repeat") or ""

        trigger_args: dict = {}
        if repeat == "daily":
            trigger_args = {"trigger": "cron", "hour": local.hour, "minute": local.minute}
        elif repeat == "weekly":
            trigger_args = {
                "trigger": "cron",
                "day_of_week": local.weekday(),
                "hour": local.hour,
                "minute": local.minute,
            }
        else:
            trigger_args = {"trigger": "date", "run_date": local}

        try:
            job = self._scheduler.add_job(
                self._fire,
                kwargs={"schedule_id": schedule_id},
                id=f"schedule-{schedule_id}",
                replace_existing=True,
                misfire_grace_time=300,
                **trigger_args,
            )
            self._job_ids[schedule_id] = job.id
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to register schedule %s: %s", schedule_id, exc)

    async def _fire(self, schedule_id: int) -> None:
        rows = await self.store.active_schedules()
        row = next((r for r in rows if int(r["id"]) == schedule_id), None)
        if row is None:
            return
        if self.notifier is not None:
            try:
                await self.notifier(int(row["chat_id"]), f"⏰ Pengingat: {row['text']}")
            except Exception as exc:  # noqa: BLE001
                log.warning("reminder delivery failed: %s", exc)
        if not row.get("repeat"):
            await self.store.deactivate_schedule(schedule_id)
            self._job_ids.pop(schedule_id, None)
