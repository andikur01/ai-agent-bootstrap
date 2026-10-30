"""Service-level tests that run real code paths (no mocks of our own logic)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from app.services.ffmpeg import resolve_ffmpeg
from app.services.media_qa import check_audio, check_html, check_image, check_video
from app.services.n8n_service import N8nService
from app.services.scheduler_service import parse_reminder
from app.services.web_builder import WebBuilder, WebProject

ffmpeg_required = pytest.mark.skipif(
    not resolve_ffmpeg().available, reason="ffmpeg not installed"
)


# ------------------------------------------------------------------ media QA
def test_check_image_rejects_blank_and_accepts_textured(tmp_path):
    blank = tmp_path / "blank.png"
    Image.new("RGB", (512, 512), (255, 255, 255)).save(blank)
    verdict = check_image(blank)
    assert not verdict.ok
    assert any("polos" in r or "putih" in r for r in verdict.reasons)

    import random

    textured = tmp_path / "textured.png"
    img = Image.new("RGB", (512, 512))
    pixels = img.load()
    for y in range(512):
        for x in range(512):
            pixels[x, y] = (
                random.randint(0, 255),
                random.randint(0, 255),
                random.randint(0, 255),
            )
    img.save(textured)
    assert check_image(textured).ok


def test_check_image_flags_low_resolution(tmp_path):
    small = tmp_path / "small.png"
    import random

    img = Image.new("RGB", (64, 64))
    pixels = img.load()
    for y in range(64):
        for x in range(64):
            pixels[x, y] = (random.randint(0, 255), 0, random.randint(0, 255))
    img.save(small)
    verdict = check_image(small, min_side=256)
    assert not verdict.ok
    assert any("resolusi" in r for r in verdict.reasons)


def test_check_html_flags_lorem_and_missing_seo(tmp_path):
    bad = tmp_path / "index.html"
    bad.write_text("<html><body><p>Lorem ipsum dolor sit amet " + "x" * 900 + "</p></body></html>")
    verdict = check_html(bad)
    assert not verdict.ok
    reasons = " ".join(verdict.reasons)
    assert "lorem ipsum" in reasons
    assert "title" in reasons
    assert "viewport" in reasons


def test_check_html_accepts_a_real_page(tmp_path):
    good = tmp_path / "index.html"
    good.write_text(
        '<!DOCTYPE html><html lang="id"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Kopi Nusantara</title></head><body><h1>Kopi</h1>"
        "<p>" + "Kami menyangrai biji kopi single origin. " * 30 + "</p></body></html>"
    )
    assert check_html(good).ok


@ffmpeg_required
def test_check_audio_rejects_silence(tmp_path):
    from app.services.audio_service import AudioService

    settings = _settings()
    service = AudioService(settings, outputs_dir=tmp_path)
    import asyncio

    path = asyncio.run(service.synth_tone(frequency=0, seconds=1.5))
    # a 0 Hz sine is silence: QA must catch it
    verdict = check_audio(path)
    assert not verdict.ok


@ffmpeg_required
def test_check_video_rejects_audio_only_file(tmp_path):
    from app.services.audio_service import AudioService

    service = AudioService(_settings(), outputs_dir=tmp_path)
    import asyncio

    audio = asyncio.run(service.synth_tone(seconds=2.0))
    verdict = check_video(audio)
    assert not verdict.ok


# ------------------------------------------------------------------ web build
def test_web_project_reports_missing_files(tmp_path):
    project = WebProject(root=tmp_path, kind="static", title="x", description="")
    assert any("tidak ada file" in issue for issue in project.issues())


def test_web_project_flags_banned_content(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><head><title>t</title><meta name='viewport' content='w'></head>"
        "<body>Lorem ipsum <img src='a.png'></body></html>"
    )
    project = WebProject(
        root=tmp_path, kind="static", title="t", description="", files=[tmp_path / "index.html"]
    )
    issues = " ".join(project.issues())
    assert "lorem ipsum" in issues
    assert "img tanpa alt" in issues


def test_web_builder_rejects_path_traversal(tmp_path, settings):
    builder = WebBuilder(settings, gateway=None, outputs_dir=tmp_path)
    spec = {
        "title": "Evil",
        "files": [
            {"path": "../../etc/passwd", "content": "pwned"},
            {"path": "index.html", "content": "<html><title>x</title><body>ok</body></html>"},
        ],
    }
    project = builder._materialise(spec, "static", "evil")
    written = {p.name for p in project.files}
    assert "passwd" not in written
    assert all(str(p).startswith(str(project.root)) for p in project.files)


# ---------------------------------------------------------------------- n8n
def test_n8n_assembles_valid_document(settings, tmp_path):
    service = N8nService(settings, gateway=None, outputs_dir=tmp_path)
    # Build the plan the way the real flow does: _validate adds the trigger.
    plan = service._validate(
        {
            "name": "Daily Report",
            "trigger": {"kind": "schedule"},
            "nodes": [
                {"key": "http", "type": "http_request", "name": "Ambil data", "params": {"url": "https://x"}},
                {"key": "ai", "type": "openai", "name": "Rangkum", "params": {}},
                {"key": "tg", "type": "telegram", "name": "Kirim", "params": {}},
            ],
            "connections": [
                {"from": "trigger", "to": "http"},
                {"from": "http", "to": "ai"},
                {"from": "ai", "to": "tg"},
            ],
        },
        "daily report",
    )
    document = service.to_n8n_json(plan)
    assert len(document["nodes"]) == 4, "trigger + 3 nodes"
    assert document["nodes"][0]["type"] == "n8n-nodes-base.scheduleTrigger"
    assert all("typeVersion" in node for node in document["nodes"])
    assert document["connections"]["Ambil data"]["main"][0][0]["node"] == "Rangkum"
    assert document["active"] is False
    saved = service.save(plan)
    assert json.loads(saved.read_text())["name"] == "Daily Report"


def test_n8n_drops_unknown_node_types(settings, tmp_path):
    service = N8nService(settings, gateway=None, outputs_dir=tmp_path)
    plan = service._validate(
        {
            "trigger": {"kind": "manual"},
            "nodes": [
                {"key": "evil", "type": "exec_shell", "name": "evil"},
                {"key": "ok", "type": "no_op", "name": "Aman"},
            ],
            "connections": [],
        },
        "test",
    )
    types = [node["type"] for node in plan.nodes]
    assert "exec_shell" not in types
    assert "no_op" in types
    assert plan.issues


def test_n8n_sanitises_dangerous_code_node(settings, tmp_path):
    service = N8nService(settings, gateway=None, outputs_dir=tmp_path)
    plan = service._validate(
        {
            "trigger": {"kind": "manual"},
            "nodes": [
                {
                    "key": "c",
                    "type": "code",
                    "name": "Code",
                    "params": {"jsCode": "const cp = require('child_process'); cp.exec('rm -rf /')"},
                }
            ],
            "connections": [],
        },
        "test",
    )
    code = next(node for node in plan.nodes if node["type"] == "code")
    assert code["params"]["jsCode"] == "return items;"


def test_n8n_linear_fallback_when_no_connections(settings, tmp_path):
    service = N8nService(settings, gateway=None, outputs_dir=tmp_path)
    plan = service._validate(
        {
            "trigger": {"kind": "manual"},
            "nodes": [{"key": "a", "type": "no_op", "name": "A"}, {"key": "b", "type": "no_op", "name": "B"}],
            "connections": [],
        },
        "test",
    )
    assert len(plan.connections) == 2


# ---------------------------------------------------------------- scheduler
@pytest.mark.parametrize(
    "text,expect_hour,expect_minute",
    [
        ("ingatkan 10 menit lagi minum air", 9, 10),
        ("dalam 2 jam cek oven", 11, 0),
        ("jam 20:30 telepon ibu", 20, 30),
        ("besok jam 7 pagi olahraga", 7, 0),
    ],
)
def test_parse_reminder_absolute_time(text, expect_hour, expect_minute):
    now = datetime(2026, 10, 30, 9, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
    parsed = parse_reminder(text, tz="Asia/Jakarta", now=now)
    assert parsed.when.hour == expect_hour
    assert parsed.when.minute == expect_minute


def test_parse_reminder_keeps_message_body_clean():
    now = datetime(2026, 10, 30, 9, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
    parsed = parse_reminder("ingatkan 10 menit lagi minum air", tz="Asia/Jakarta", now=now)
    assert parsed.text == "minum air", parsed.text


def test_parse_reminder_repeat_daily():
    now = datetime(2026, 10, 30, 9, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
    parsed = parse_reminder("setiap hari jam 8 minum vitamin", tz="Asia/Jakarta", now=now)
    assert parsed.repeat == "daily"
    assert parsed.text == "minum vitamin"


def test_parse_reminder_without_time_raises():
    from app.core.errors import ToolError

    with pytest.raises(ToolError):
        parse_reminder("ingatkan aku ya", tz="Asia/Jakarta")


# -------------------------------------------------------------------- deploy
def test_zip_project_bundles_files(tmp_path):
    from app.services.deploy_service import zip_project

    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<html></html>")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x")
    archive = zip_project(root)
    import zipfile

    with zipfile.ZipFile(archive) as handle:
        names = handle.namelist()
    assert "index.html" in names
    assert not any("node_modules" in name for name in names)


# ---------------------------------------------------------------- presenter
def test_split_message_respects_limit_and_boundaries():
    from app.bot.presenter import split_message

    text = "\n\n".join(f"Paragraf {i} " + "kata " * 40 for i in range(30))
    chunks = split_message(text, limit=1000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).count("Paragraf") == 30


@pytest.mark.asyncio
async def test_send_text_redacts_secrets_before_sending():
    from app.bot.presenter import send_text

    sent: list[str] = []

    class FakeMessage:
        async def reply_text(self, text, **kwargs):
            sent.append(text)

    secret = "sk-" + "a" * 32
    await send_text(FakeMessage(), f"ini token aku {secret} jangan bilang siapa-siapa")
    assert sent
    assert all(secret not in chunk for chunk in sent)
    assert "REDACTED" in sent[0]


def _settings():
    from app.core.config import get_settings

    return get_settings()
