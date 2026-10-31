"""End-to-end pipeline tests with a stubbed gateway.

These exercise the real orchestration code: web build -> QA -> ship, the
orchestrator's route/execute path, and the confirmation gate. The only thing
faked is the LLM, because a test suite must not spend money or depend on a
provider being up.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.intent import Intent, Route
from app.core.orchestrator import Orchestrator
from app.db.store import Store
from app.llm.budget import BudgetGuard
from app.llm.cache import SemanticCache
from app.llm.gateway import LLMResult
from app.memory.artifacts import ArtifactRegistry
from app.memory.embeddings import build_embedder
from app.memory.manager import MemoryManager
from app.memory.vector_store import build_vector_store
from app.services.browser_qa import browser_ready
from app.services.deploy_service import DeployService
from app.services.web_builder import WebBuilder
from app.services.web_repair import WebRepairService

GOOD_HTML = """<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Warung Kopi Gayo</title>
<meta name="description" content="Warung kopi spesialti dengan biji Gayo, Toraja, dan Bajawa, diseduh manual setiap hari.">
<style>body{font-family:system-ui;margin:0;background:#0f172a;color:#e2e8f0}h1{padding:2rem}main{padding:0 2rem 3rem;max-width:70ch;line-height:1.6}</style>
</head><body><h1>Warung Kopi Gayo</h1><main>
<p>Kami menyangrai biji single origin dari dataran tinggi Gayo, Toraja, dan Bajawa setiap pekan lalu menyeduhnya manual dengan V60 dan Aeropress.</p>
<p>Buka 07.00-22.00 di Jalan Cikini Raya 12, Jakarta. Kelas seduh manual setiap Sabtu pukul 10.00 bersama barista bersertifikat.</p>
<p>Menu andalan: Gayo natural, Toraja washed, dan es kopi susu gula aren dari biji Kintamani.</p>
</main></body></html>"""


class StubGateway:
    """Minimal LLM stand-in: answers JSON for planning, text otherwise."""

    def __init__(self, payload: dict | None = None, text: str = "Jawaban singkat."):
        self.payload = payload or {}
        self.text = text
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def require_ready(self) -> None:
        return None

    async def complete(self, messages, **kwargs) -> LLMResult:
        self.calls.append("complete")
        if kwargs.get("json_mode"):
            body = json.dumps(self.payload)
        else:
            body = self.text
        return LLMResult(text=body, model="stub", provider="stub", prompt_tokens=10, completion_tokens=20)

    async def complete_json(self, messages, **kwargs) -> dict:
        self.calls.append("complete_json")
        return dict(self.payload)


@pytest.fixture
def stub_web_gateway():
    return StubGateway(
        payload={
            "title": "Warung Kopi Gayo",
            "description": "Kopi spesialti",
            "files": [{"path": "index.html", "content": GOOD_HTML}],
            "notes": "ok",
        }
    )


@pytest.fixture
async def orchestrator(tmp_path, settings):
    store = Store(tmp_path / "test.sqlite3")
    embedder = build_embedder(settings)
    vectors = build_vector_store(settings, store, embedder)
    gateway = StubGateway(
        text="Halo, ini jawaban uji yang cukup panjang supaya layak masuk cache semantik."
    )
    memory = MemoryManager(store, vectors, ArtifactRegistry(store), settings, gateway=gateway)
    cache = SemanticCache(store, embedder, enabled=True)
    await cache.init()
    orch = Orchestrator(
        settings=settings,
        store=store,
        gateway=gateway,
        memory=memory,
        cache=cache,
        budget=BudgetGuard(store, settings.daily_token_budget),
    )
    orch.web = WebBuilder(settings, gateway=gateway, outputs_dir=tmp_path / "outputs")
    orch.qa = None
    yield orch
    store.close()


@pytest.mark.asyncio
async def test_chat_uses_cache_on_second_identical_question(orchestrator):
    route = Route(Intent.CHAT, {"text": "apa kabar bot"})
    first = await orchestrator.execute(1, route)
    assert first.cached is False
    second = await orchestrator.execute(1, Route(Intent.CHAT, {"text": "apa kabar bot"}))
    assert second.cached is True
    assert second.text == first.text


@pytest.mark.asyncio
async def test_chat_remembers_both_turns(orchestrator):
    # Mirrors the real Telegram flow: the handler persists the user turn before
    # routing and the assistant turn after the outcome comes back.
    await orchestrator.memory.remember_message(1, "user", "halo bot")
    outcome = await orchestrator.execute(1, Route(Intent.CHAT, {"text": "halo bot"}))
    await orchestrator.memory.remember_message(1, "assistant", outcome.text)
    stats = await orchestrator.memory.stats(1)
    assert stats["messages"] >= 2
    history = await orchestrator.memory.store.recent_messages(1, limit=5)
    roles = {row["role"] for row in history}
    assert roles == {"user", "assistant"}


@pytest.mark.asyncio
async def test_image_edit_without_artifact_asks_instead_of_crashing(orchestrator):
    outcome = await orchestrator.execute(1, Route(Intent.IMAGE_EDIT, {"instruction": "perbaiki"}))
    assert "Belum ada gambar" in outcome.text


@pytest.mark.asyncio
async def test_reminder_without_scheduler_is_reported(orchestrator):
    outcome = await orchestrator.execute(
        1, Route(Intent.REMINDER, {"text": "ingatkan 10 menit lagi minum air"}), chat_id=1
    )
    assert "Scheduler" in outcome.text or "Pengingat diset" in outcome.text


@pytest.mark.asyncio
async def test_memory_write_and_stats(orchestrator):
    written = await orchestrator.execute(
        1, Route(Intent.MEMORY_QUERY, {"mode": "write", "text": "ingat aku suka kopi pahit"})
    )
    assert "kopi pahit" in written.text
    stats = await orchestrator.execute(1, Route(Intent.MEMORY_QUERY, {"mode": "stats"}))
    assert "Status memori" in stats.text


@pytest.mark.asyncio
async def test_link_ingest_without_url_asks_for_one(orchestrator):
    outcome = await orchestrator.execute(1, Route(Intent.LINK_INGEST, {"urls": [], "question": ""}))
    assert "Kirim link" in outcome.text


@pytest.mark.asyncio
async def test_thinking_engine_produces_all_passes(orchestrator):
    outcome = await orchestrator.execute(1, Route(Intent.THINK, {"text": "kenapa langit biru"}))
    assert "FRAME" in outcome.text or "Jawaban" in outcome.text
    assert outcome.tokens > 0


@pytest.mark.asyncio
async def test_web_build_to_qa_to_bundle(tmp_path, settings, stub_web_gateway):
    builder = WebBuilder(settings, gateway=stub_web_gateway, outputs_dir=tmp_path / "outputs")
    project = await builder.build("website kedai kopi Gayo", kind="static")
    assert [p.name for p in project.files] == ["index.html"]
    assert project.issues() == []

    service = WebRepairService(
        settings,
        builder=builder,
        deployer=DeployService(settings),
        qa=__import__("app.services.browser_qa", fromlist=["BrowserQA"]).BrowserQA(
            tmp_path / "outputs"
        ),
        max_rounds=2,
    )
    if not browser_ready():
        pytest.skip("chromium browser not installed")

    result = await service.ship(project, project_name=project.title)
    assert result.ok, result.summary()
    assert result.bundle and Path(result.bundle).exists()
    assert len(result.final_report.screenshots()) >= 1
    for shot in result.final_report.screenshots():
        assert Path(shot).exists()


@pytest.mark.asyncio
async def test_web_repair_loop_fixes_a_broken_page(tmp_path, settings):
    """A page with a JS error must be flagged, repaired, and then pass."""
    broken = "<!DOCTYPE html><html lang='id'><head><meta charset='utf-8'>" \
             "<meta name='viewport' content='width=device-width,initial-scale=1'>" \
             "<title>Rusak</title></head><body><script>throw new Error('boom')</script></body></html>"
    gateway = StubGateway(
        payload={
            "title": "Warung Kopi Gayo",
            "description": "Kopi spesialti",
            "files": [{"path": "index.html", "content": broken}],
        }
    )
    builder = WebBuilder(settings, gateway=gateway, outputs_dir=tmp_path / "outputs")
    project = await builder.build("website rusak", kind="static")

    from app.services.browser_qa import BrowserQA

    if not browser_ready():
        pytest.skip("chromium browser not installed")

    service = WebRepairService(
        settings,
        builder=builder,
        deployer=DeployService(settings),
        qa=BrowserQA(tmp_path / "outputs"),
        max_rounds=3,
    )
    # first round QA fails, repair replaces content with the good page
    gateway.payload = {
        "title": "Warung Kopi Gayo",
        "description": "Kopi spesialti",
        "files": [{"path": "index.html", "content": GOOD_HTML}],
    }
    result = await service.ship(project, project_name=project.title)
    assert len(result.rounds) >= 2, "should have QA'd the broken build first"
    assert result.ok, result.summary()
