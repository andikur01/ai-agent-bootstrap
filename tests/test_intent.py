"""Intent routing and progress/confirmation tests."""
from __future__ import annotations

import pytest

from app.core.intent import Intent, Route, classify_rules, route_message
from app.core.progress import ConfirmationGate, ProgressReporter, parse_decision


@pytest.mark.parametrize(
    "text,expected",
    [
        ("buatkan gambar kucing astronot", Intent.IMAGE_CREATE),
        ("bikin aku poster film horor", Intent.IMAGE_CREATE),
        ("edit gambar yang tadi jadi lebih terang", Intent.IMAGE_EDIT),
        ("bikin video animasi pythagoras", Intent.VIDEO_CREATE),
        ("potong video tadi jadi 10 detik", Intent.VIDEO_EDIT),
        ("buat musik lo-fi untuk belajar", Intent.AUDIO_CREATE),
        ("bacakan ini jadi suara", Intent.TTS),
        ("transkrip audio ini dong", Intent.STT),
        ("buat website portofolio", Intent.WEB_STATIC),
        ("buat web app dengan login dan database", Intent.WEB_FULLSTACK),
        ("perbaiki website yang error", Intent.WEB_REPAIR),
        ("buatkan workflow n8n kirim email", Intent.N8N),
        ("belajar tentang kubernetes", Intent.LEARN),
        ("ingatkan saya besok jam 9", Intent.REMINDER),
        ("https://youtu.be/abc123 jelaskan isinya", Intent.LINK_INGEST),
        ("lanjutkan yang tadi", Intent.CONTINUE),
        ("lanjut yang kemarin", Intent.CONTINUE),
        ("teruskan", Intent.CONTINUE),
    ],
)
def test_rule_routing(text, expected):
    route = classify_rules(text)
    assert route is not None, text
    assert route.intent == expected, f"{text} -> {route.intent}"


def test_continuation_with_new_instruction_is_not_continue():
    # "perbaiki gambarnya jadi gelap" carries an instruction, so it must be an
    # edit, not a bare pointer at previous work.
    route = classify_rules("edit gambar yang tadi jadi lebih terang")
    assert route.intent == Intent.IMAGE_EDIT


@pytest.mark.parametrize(
    "text,expected",
    [
        ("edit gambarnya jadi gelap", Intent.IMAGE_EDIT),
        ("ubah fotonya jadi hitam putih", Intent.IMAGE_EDIT),
        ("perbaiki videonya jadi lambat", Intent.VIDEO_EDIT),
        ("ubah suaranya lebih cepat", Intent.AUDIO_EDIT),
        ("perbaiki websitenya", Intent.WEB_REPAIR),
    ],
)
def test_indonesian_suffixes_still_route_to_edits(text, expected):
    # "gambarnya" / "videonya" must not slip through the word-boundary rule.
    route = classify_rules(text)
    assert route is not None, text
    assert route.intent == expected, f"{text} -> {route.intent}"


def test_plain_chat_has_no_rule_route():
    assert classify_rules("halo apa kabar") is None


def test_media_attachments_route_to_edit_and_stt():
    photo = classify_rules("perbaiki ini", has_photo=True)
    assert photo.intent == Intent.IMAGE_EDIT
    voice = classify_rules("", has_voice=True)
    assert voice.intent == Intent.STT


def test_heavy_intents_require_confirmation():
    route = classify_rules("buatkan gambar kucing")
    assert route.needs_confirm is True
    light = Route(Intent.CHAT, {"text": "hi"})
    assert light.needs_confirm is False


def test_url_extraction_keeps_question():
    route = classify_rules("https://youtu.be/x tolong ringkas video ini")
    assert route.args["urls"] == ["https://youtu.be/x"]
    assert "ringkas" in route.args["question"]


@pytest.mark.asyncio
async def test_route_message_falls_back_to_chat_without_gateway():
    route = await route_message("halo halo halo", None)
    assert route.intent == Intent.CHAT


@pytest.mark.parametrize(
    "text,expected",
    [("ya", True), ("Ya", True), ("iya", True), ("gas", True), ("tidak", False), ("gak", False), ("batal", False), ("hmm", None)],
)
def test_parse_decision(text, expected):
    assert parse_decision(text) is expected


def test_progress_bar_rendering():
    assert ProgressReporter.render_bar(0) == "░" * 12
    assert ProgressReporter.render_bar(100) == "█" * 12
    assert ProgressReporter.render_bar(50).count("█") == 6


@pytest.mark.asyncio
async def test_confirmation_gate_resolves_and_times_out():
    gate = ConfirmationGate(timeout_seconds=1)
    pending = gate.create(1, "image_create", {"text": "x"})
    assert gate.resolve(1, True) is True
    assert await gate.wait(pending) is True

    pending = gate.create(2, "video_create", {})
    assert gate.cancel(2) is None
    assert await gate.wait(pending) is False
    assert gate.resolve(999, True) is False


@pytest.mark.asyncio
async def test_progress_reporter_throttles_and_edits():
    sent: list[str] = []
    edits: list[str] = []

    class FakeMessage:
        async def edit_text(self, text):
            edits.append(text)

    async def send(text):
        sent.append(text)
        return FakeMessage()

    reporter = ProgressReporter(send, title="test")
    await reporter.update(5, "start", force=True)
    await reporter.update(6, "noise")  # below both thresholds -> dropped
    await reporter.update(40, "half", force=True)
    await reporter.done("finish")
    assert len(sent) == 1, "only the first update creates a message"
    assert len(edits) == 2
    assert "100%" in edits[-1]


@pytest.mark.asyncio
async def test_progress_reporter_sends_screenshot_even_when_throttled():
    photos: list[tuple[str, str]] = []

    async def send(text):
        return None

    async def send_photo(path, caption):
        photos.append((path, caption))

    reporter = ProgressReporter(send, title="qa", send_photo=send_photo)
    await reporter.update(10, "start", force=True)
    await reporter.update(11, "milestone", screenshot="/tmp/shot.png")
    assert photos and photos[0][0] == "/tmp/shot.png"
    assert "11%" in photos[0][1]
