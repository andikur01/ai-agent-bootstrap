"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep tests hermetic: no real .env, no network, deterministic settings.
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "111,222")
os.environ.setdefault("TELEGRAM_ADMIN_USER_IDS", "111")
os.environ.setdefault("DAILY_TOKEN_BUDGET", "100000")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    settings = config_module.get_settings()
    yield settings
    config_module.get_settings.cache_clear()


@pytest.fixture()
def store(settings, tmp_path):
    from app.db.store import Store

    return Store(tmp_path / "test.sqlite3")


@pytest.fixture()
def embedder(settings):
    from app.memory.embeddings import HashEmbedder

    return HashEmbedder(dim=settings.embedding_dim)


@pytest.fixture()
def vectors(store, embedder):
    from app.memory.vector_store import SQLiteVectorStore

    return SQLiteVectorStore(store, embedder)


@pytest.fixture()
def registry(store):
    from app.memory.artifacts import ArtifactRegistry

    return ArtifactRegistry(store)


@pytest.fixture()
def memory(store, vectors, registry, settings):
    from app.memory.manager import MemoryManager

    return MemoryManager(store, vectors, registry, settings)


@pytest.fixture()
def gateway(settings, store):
    from app.llm.budget import BudgetGuard
    from app.llm.gateway import LLMGateway

    return LLMGateway(settings, budget=BudgetGuard(store, settings.daily_token_budget))
