"""Keeping the disk from filling, without eating anything irreplaceable.

The failure this guards against is a bot that runs for weeks and then dies
because it filled its own disk with renders. The failure it must not cause is
deleting a saved login or the memory database to buy space.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services.retention import Retention, RetentionPolicy, build_retention


def _write(path: Path, megabytes: float, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * int(megabytes * 1024 * 1024))
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def tree(tmp_path):
    outputs = tmp_path / "outputs"
    data = tmp_path / "data"
    (outputs / "images").mkdir(parents=True)
    (outputs / "video").mkdir(parents=True)
    (data / "profiles").mkdir(parents=True)
    (data / "uploads").mkdir(parents=True)
    return outputs, data


def test_old_files_are_deleted_and_recent_ones_survive(tree):
    outputs, data = tree
    for index in range(6):
        _write(outputs / "images" / f"old{index}.png", 0.5, age_days=40 + index)
    _write(outputs / "images" / "fresh.png", 0.5)

    retention = Retention(outputs, data, RetentionPolicy(max_total_mb=100, max_age_days=14))
    report = retention.sweep()

    assert report.deleted, "old files past the age limit should be removed"
    assert (outputs / "images" / "fresh.png").exists()
    assert "fresh.png" not in " ".join(report.deleted)


def test_a_saved_login_is_never_deleted(tree):
    """Deleting this logs the user out of every site they set up."""
    outputs, data = tree
    cookie = _write(data / "profiles" / "example.com" / "cookies.json", 2.0, age_days=999)
    _write(outputs / "images" / "big.png", 5.0, age_days=999)

    retention = Retention(outputs, data, RetentionPolicy(max_total_mb=1, max_age_days=1))
    retention.sweep()

    assert cookie.exists(), "a saved login must survive any sweep"


def test_the_memory_database_is_never_deleted(tree):
    """Deleting this loses everything the bot was supposed to remember."""
    outputs, data = tree
    database = _write(data / "agent.sqlite3", 0.5, age_days=999)

    retention = Retention(outputs, data, RetentionPolicy(max_total_mb=0, max_age_days=1))
    retention.sweep()

    assert database.exists()


def test_an_env_file_is_never_deleted(tree):
    outputs, data = tree
    env = _write(data / "uploads" / "backup.env", 0.1, age_days=999)

    retention = Retention(outputs, data, RetentionPolicy(max_total_mb=0, max_age_days=1))
    retention.sweep()

    assert env.exists()


def test_the_newest_file_of_each_kind_survives_even_when_old(tree):
    """The user is most likely to still mean the thing they just made."""
    outputs, data = tree
    newest_image = _write(outputs / "images" / "latest.png", 3.0, age_days=500)
    newest_video = _write(outputs / "video" / "latest.mp4", 3.0, age_days=500)
    _write(outputs / "images" / "older.png", 3.0, age_days=600)

    retention = Retention(
        outputs, data, RetentionPolicy(max_total_mb=1, max_age_days=1, keep_latest_per_kind=1)
    )
    retention.sweep()

    assert newest_image.exists()
    assert newest_video.exists()


def test_a_sweep_under_the_limits_deletes_nothing(tree):
    outputs, data = tree
    keep = _write(outputs / "images" / "small.png", 0.1, age_days=1)

    retention = Retention(outputs, data, RetentionPolicy(max_total_mb=100, max_age_days=14))
    report = retention.sweep()

    assert report.deleted == []
    assert keep.exists()
    assert "tidak ada yang dihapus" in report.summary()


def test_size_alone_triggers_deletion(tree):
    """Age is not the only reason to clean up: a burst of large renders can
    fill a small disk in a single day."""
    outputs, data = tree
    for index in range(6):
        _write(outputs / "images" / f"today{index}.png", 2.0)

    retention = Retention(
        outputs, data, RetentionPolicy(max_total_mb=4, max_age_days=999, keep_latest_per_kind=1)
    )
    report = retention.sweep()

    assert report.deleted, "being over the size limit should trigger a sweep"
    size_mb = sum(p.stat().st_size for p in (outputs / "images").iterdir()) / (1024 * 1024)
    assert size_mb <= 4


def test_disabling_the_policy_stops_the_sweep(tree):
    outputs, data = tree
    old = _write(outputs / "images" / "old.png", 1.0, age_days=999)
    _write(outputs / "images" / "newer.png", 1.0, age_days=900)

    retention = Retention(
        outputs,
        data,
        RetentionPolicy(max_total_mb=0, max_age_days=1, enabled=False, keep_latest_per_kind=1),
    )
    assert retention.sweep().deleted == []
    assert old.exists()

    # An explicit request still cleans, so the user is never stuck.
    assert retention.sweep(force=True).deleted


def test_an_explicit_clean_still_protects_the_newest_file(tree):
    """force overrides the on/off switch, not the safety rules. Deleting the
    file the user just made because they asked to tidy up would be a nasty
    surprise."""
    outputs, data = tree
    only = _write(outputs / "images" / "latest.png", 5.0, age_days=500)

    retention = Retention(
        outputs, data, RetentionPolicy(max_total_mb=0, max_age_days=1, keep_latest_per_kind=1)
    )
    retention.sweep(force=True)

    assert only.exists()


def test_the_status_line_reports_a_percentage(tree):
    outputs, data = tree
    _write(outputs / "images" / "half.png", 1.0)

    retention = Retention(outputs, data, RetentionPolicy(max_total_mb=2))
    status = retention.status()

    assert "1 MB" in status or "1.0 MB" in status
    assert "%" in status


def test_a_missing_directory_does_not_crash(tmp_path):
    retention = Retention(tmp_path / "nope", tmp_path / "gone", RetentionPolicy())
    assert retention.sweep().deleted == []


def test_settings_build_a_matching_policy(tmp_path, monkeypatch):
    from app.core.config import Settings

    settings = Settings(
        outputs_max_mb=512,
        outputs_max_age_days=7,
        outputs_dir=str(tmp_path / "out"),
        data_dir=str(tmp_path / "dat"),
    )
    retention = build_retention(settings)

    assert retention.policy.max_total_mb == 512
    assert retention.policy.max_age_days == 7


# ---------------------------------------------------------------------------
# Uptime behaviour. The user reported the symptom directly: they messaged from
# their phone and got nothing. Two causes, both fixed here.
# ---------------------------------------------------------------------------


def test_pending_updates_are_not_dropped_on_startup():
    """The bug the user actually hit.

    The bot was starting with drop_pending_updates=True, so every message sent
    while it was down was deleted before it could be read. A real message was
    found sitting in the Telegram queue during this fix - proof the queue works
    and the old setting was throwing messages away.
    """
    from pathlib import Path

    source = Path("app/application.py").read_text(encoding="utf-8")
    assert "drop_pending_updates=False" in source
    assert "drop_pending_updates=True" not in source, (
        "dropping pending updates loses messages sent while the bot was down"
    )


def test_the_startup_announcement_is_wired_up():
    source = Path("app/application.py").read_text(encoding="utf-8")
    assert "_announce_startup" in source


def test_retention_is_scheduled_so_the_disk_cannot_fill_silently():
    source = Path("app/services/scheduler_service.py").read_text(encoding="utf-8")
    assert "_register_retention" in source
    assert "disk-retention" in source


def test_a_service_unit_restarts_the_bot():
    """Without Restart=always the bot stays down until someone notices."""
    unit = Path("deploy/ai-agent.service").read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "WantedBy=multi-user.target" in unit


def test_the_runner_script_exists_and_is_executable():
    import os

    path = Path("deploy/run-forever.sh")
    assert path.exists()
    assert os.access(path, os.X_OK), "the runner must be executable to be useful"
