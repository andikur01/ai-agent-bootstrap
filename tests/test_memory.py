"""Memory tests: the "perbaiki yang tadi" contract."""
from __future__ import annotations

import pytest

from app.memory.artifacts import KIND_AUDIO, KIND_IMAGE, KIND_VIDEO, KIND_WEB
from app.memory.resolver import looks_like_reference


def test_looks_like_reference_detects_followups():
    for text in (
        "perbaiki yang tadi",
        "lanjutkan itu",
        "yang kemarin tolong diubah",
        "fix the last one",
        "revisi sebelumnya",
        "tambahkan musik",
    ):
        assert looks_like_reference(text), text


def test_long_message_is_not_a_reference():
    assert not looks_like_reference("buatkan " + "detail " * 60)


@pytest.mark.asyncio
async def test_resolves_to_latest_of_named_kind(memory):
    await memory.register_artifact(1, KIND_WEB, title="Portofolio", location="/tmp/web/index.html")
    await memory.register_artifact(1, KIND_IMAGE, title="Logo", location="/tmp/img/logo.png")

    ref = await memory.resolve_reference(1, "edit gambar yang tadi")
    assert ref.is_reference and ref.artifact is not None
    assert ref.artifact.kind == KIND_IMAGE

    ref = await memory.resolve_reference(1, "perbaiki website tadi")
    assert ref.artifact is not None and ref.artifact.kind == KIND_WEB


@pytest.mark.asyncio
async def test_named_kind_without_artifact_asks_instead_of_guessing(memory):
    await memory.register_artifact(1, KIND_WEB, title="Situs", location="/tmp/web/index.html")
    ref = await memory.resolve_reference(1, "lanjutkan video yang tadi")
    assert ref.is_reference
    assert ref.artifact is None
    assert ref.needs_clarification, "harus minta klarifikasi, bukan mengedit web"


@pytest.mark.asyncio
async def test_bare_reference_uses_most_recent_artifact(memory):
    await memory.register_artifact(1, KIND_VIDEO, title="V1", location="/tmp/v1.mp4")
    await memory.register_artifact(1, KIND_VIDEO, title="V2", location="/tmp/v2.mp4")
    ref = await memory.resolve_reference(1, "perbaiki yang tadi")
    assert ref.artifact is not None
    assert ref.artifact.title == "V2"


@pytest.mark.asyncio
async def test_kemarin_window_filters(memory):
    await memory.register_artifact(1, KIND_IMAGE, title="Lama", location="/tmp/old.png")
    # backdate the first artifact by 5 days
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    memory.store._exec("UPDATE artifacts SET created_at = ? WHERE title = 'Lama'", (old,))
    await memory.register_artifact(1, KIND_IMAGE, title="Baru", location="/tmp/new.png")

    ref = await memory.resolve_reference(1, "gambar kemarin")
    assert ref.artifact is not None
    assert ref.artifact.title == "Baru"


@pytest.mark.asyncio
async def test_non_reference_message_not_flagged(memory):
    ref = await memory.resolve_reference(1, "buatkan aku puisi tentang hujan")
    assert not ref.is_reference


@pytest.mark.asyncio
async def test_context_is_bounded_and_includes_reference(memory, settings):
    for i in range(40):
        await memory.remember_message(1, "user", f"pesan lama nomor {i} " + "x" * 200)
    await memory.register_artifact(1, KIND_AUDIO, title="Voice", location="/tmp/v.mp3")

    ref = await memory.resolve_reference(1, "edit suara yang tadi")
    bundle = await memory.build_context(1, "edit suara yang tadi", reference=ref)

    assert bundle.tokens <= settings.memory_max_context_tokens + 4000
    assert "REFERENSI USER" in bundle.system
    assert "Voice" in bundle.system
    # must not ship the whole history
    assert len(bundle.messages) < 25


@pytest.mark.asyncio
async def test_learning_is_recallable(memory):
    await memory.remember_learning(
        1, "fastapi", "FastAPI adalah framework Python async untuk API.", ["https://fastapi.tiangolo.com"]
    )
    hits = await memory.learnings(1, "apa itu fastapi")
    assert hits and "FastAPI" in hits[0].text
    stored = await memory.store.list_learnings(1)
    assert stored and stored[0]["topic"] == "fastapi"


@pytest.mark.asyncio
async def test_stats_and_artifact_listing(memory):
    await memory.remember_message(1, "user", "halo")
    await memory.register_artifact(1, KIND_IMAGE, title="A", location="/tmp/a.png")
    stats = await memory.stats(1)
    assert stats["messages"] >= 1
    assert stats["artifacts"] >= 1
    line = await memory.registry.summary_line(1)
    assert "image" in line
