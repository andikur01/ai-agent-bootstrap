"""Embedding providers.

Order of preference:
1. Hugging Face Inference API (needs ``HF_TOKEN``) - good quality, no local RAM.
2. Local ``sentence-transformers`` when the package + weights are present.
3. Deterministic hashing embedder - always available, offline, and good enough
   for dedup/similarity on short texts. Keeps the bot functional with zero keys.

The fallback matters: memory and semantic cache must never be the reason the bot
refuses to start.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from app.core.logging_setup import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Hashing trick embedder: token hashed into buckets, then L2-normalised.

    Not semantic in the deep-learning sense, but it captures lexical overlap,
    which is what the cache dedup and "same topic?" checks actually need.
    """

    name = "hash"

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return vector
        for token in tokens:
            for ngram in (token, token[:4]):
                digest = hashlib.blake2b(ngram.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[idx] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            return vector
        return [v / norm for v in vector]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class HFEmbedder:
    name = "hf"

    def __init__(self, model: str, token: str, dim: int = 384):
        self.model = model
        self.token = token
        self.dim = dim
        self.url = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"inputs": texts, "options": {"wait_for_model": True}}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self.url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        vectors: list[list[float]] = []
        for item in data:
            if isinstance(item, list) and item and isinstance(item[0], list):
                # token-level output -> mean pool
                length = len(item)
                pooled = [
                    sum(tok[i] for tok in item) / length for i in range(len(item[0]))
                ]
                vectors.append(pooled)
            elif isinstance(item, list):
                vectors.append([float(v) for v in item])
            else:
                vectors.append([0.0] * self.dim)
        return vectors


class LocalSTEmbedder:
    name = "sentence-transformers"

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model_name = model
        self._model = SentenceTransformer(model)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


class FallbackEmbedder:
    """Wraps a primary embedder and degrades to hashing on any failure."""

    def __init__(self, primary: Embedder, fallback: Embedder):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.dim = primary.dim
        self._degraded = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._degraded:
            return await self.fallback.embed(texts)
        try:
            vectors = await self.primary.embed(texts)
            if vectors and len(vectors[0]) == self.primary.dim:
                return vectors
            raise ValueError("unexpected embedding shape")
        except Exception as exc:  # noqa: BLE001
            log.warning("Embedder %s failed (%s); falling back to hashing", self.primary.name, exc)
            self._degraded = True
            self.dim = self.fallback.dim
            return await self.fallback.embed(texts)


def build_embedder(settings) -> Embedder:
    fallback = HashEmbedder(dim=settings.embedding_dim)
    if settings.hf_token:
        try:
            return FallbackEmbedder(
                HFEmbedder(settings.embedding_model, settings.hf_token, settings.embedding_dim),
                fallback,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("HF embedder unavailable: %s", exc)
    try:
        return FallbackEmbedder(LocalSTEmbedder(settings.embedding_model), fallback)
    except Exception:
        return fallback


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
