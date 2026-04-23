"""Embedding service — provider-abstracted sentence embeddings with cache.

Every utterance embedded by the pipeline goes through this module.  Used
by:

- Topic clustering (BERTopic-style) in ``topic_clustering.py``.
- LSM and semantic similarity if needed.
- RAG / search (future).
- Active-learning uncertainty sampling (distance-to-centroid).

Providers (selected via ``settings.EMBEDDING_PROVIDER``):

- ``voyage`` — Anthropic's recommended partner; uses
  ``voyage-3-large`` when available.
- ``openai`` — ``text-embedding-3-small`` (1536-d).
- ``hash`` — deterministic pseudo-embeddings (no network; reliable for
  tests and offline development; 256-d).

Caching: every ``(provider, text)`` pair is keyed by SHA-256 of the
normalized text and stored in Redis with a 24-hour TTL.  Cache misses
hit the provider; hits return the cached vector directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import struct
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────


DEFAULT_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "hash")
DEFAULT_CACHE_TTL_SEC = 24 * 3600


# ── Provider contract ────────────────────────────────────────────────────


@dataclass
class EmbeddingResult:
    vectors: List[List[float]]
    model: str
    dim: int


class EmbeddingProvider:
    """Minimal provider contract — async batch embed of strings."""

    name: str = "base"
    dim: int = 0

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        raise NotImplementedError


# ── Deterministic hash provider (test / offline) ─────────────────────────


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic, offline pseudo-embedding for dev and testing.

    Produces a unit-length 256-d vector by hashing every whitespace
    token and accumulating signed contributions per dimension.  This
    captures rough lexical similarity (same word → same contributions)
    without any model dependency.
    """

    name = "hash"
    dim = 256

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in (text or "").lower().split():
                h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                # 4 contributions per token — spread across the vector.
                for i in range(4):
                    idx = struct.unpack_from("<H", h, i * 2)[0] % self.dim
                    sign = 1.0 if (idx ^ h[i]) & 1 else -1.0
                    vec[idx] += sign
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return EmbeddingResult(vectors=vectors, model="hash-v1", dim=self.dim)


# ── Voyage / OpenAI providers (thin wrappers; gracefully degrade) ─────────


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Voyage AI embeddings — Anthropic's recommended partner.

    Lazy-imports ``voyageai`` so the backend still starts when the
    dependency is absent.  Falls back to :class:`HashEmbeddingProvider`
    if the client cannot be constructed.
    """

    name = "voyage"
    dim = 1024

    def __init__(self, api_key: Optional[str] = None, model: str = "voyage-3-large") -> None:
        self._api_key = api_key or os.environ.get("VOYAGE_API_KEY")
        self._model = model
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import voyageai  # type: ignore
            self._client = voyageai.AsyncClient(api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning("Voyage client unavailable (%s); falling back to hash provider", exc)
            self._client = None
        return self._client

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        client = self._get_client()
        if client is None:
            return await HashEmbeddingProvider().embed(texts)
        try:
            resp = await client.embed(list(texts), model=self._model)
            vectors = list(resp.embeddings)
            return EmbeddingResult(vectors=vectors, model=self._model, dim=len(vectors[0]) if vectors else 0)
        except Exception:
            logger.exception("Voyage embed failed; falling back to hash provider")
            return await HashEmbeddingProvider().embed(texts)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"
    dim = 1536

    def __init__(self, api_key: Optional[str] = None, model: str = "text-embedding-3-small") -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._model = model
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import openai  # type: ignore
            self._client = openai.AsyncOpenAI(api_key=self._api_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAI client unavailable (%s); falling back to hash provider", exc)
            self._client = None
        return self._client

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        client = self._get_client()
        if client is None:
            return await HashEmbeddingProvider().embed(texts)
        try:
            resp = await client.embeddings.create(model=self._model, input=list(texts))
            vectors = [e.embedding for e in resp.data]
            return EmbeddingResult(vectors=vectors, model=self._model, dim=len(vectors[0]) if vectors else 0)
        except Exception:
            logger.exception("OpenAI embed failed; falling back to hash provider")
            return await HashEmbeddingProvider().embed(texts)


# ── Redis cache layer ────────────────────────────────────────────────────


class EmbeddingCache:
    """Redis-backed cache keyed by SHA-256(provider + text).

    Gracefully degrades to an in-process dict when Redis is unavailable
    so the service still works in tests and local dev.
    """

    def __init__(self, redis_url: Optional[str] = None, ttl: int = DEFAULT_CACHE_TTL_SEC) -> None:
        self._ttl = ttl
        self._client: Any = None
        self._redis_url = redis_url or os.environ.get("REDIS_URL")
        self._fallback: dict = {}

    def _connect(self) -> None:
        if self._client is not None or not self._redis_url:
            return
        try:
            import redis.asyncio as redis  # type: ignore
            self._client = redis.from_url(self._redis_url, decode_responses=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis unavailable (%s); using in-process embedding cache", exc)
            self._client = None

    @staticmethod
    def _key(provider: str, text: str) -> str:
        h = hashlib.sha256(f"{provider}:{(text or '').strip().lower()}".encode("utf-8")).hexdigest()
        return f"emb:{provider}:{h}"

    async def get_many(self, provider: str, texts: Sequence[str]) -> List[Optional[List[float]]]:
        self._connect()
        keys = [self._key(provider, t) for t in texts]
        if self._client is None:
            return [self._fallback.get(k) for k in keys]
        try:
            raw = await self._client.mget(keys)
        except Exception:
            logger.exception("Redis mget failed; using fallback")
            return [self._fallback.get(k) for k in keys]
        return [json.loads(b) if b else None for b in raw]

    async def set_many(
        self,
        provider: str,
        texts: Sequence[str],
        vectors: Sequence[List[float]],
    ) -> None:
        self._connect()
        keys = [self._key(provider, t) for t in texts]
        if self._client is None:
            for k, v in zip(keys, vectors):
                self._fallback[k] = v
            return
        try:
            pipe = self._client.pipeline()
            for k, v in zip(keys, vectors):
                pipe.setex(k, self._ttl, json.dumps(v))
            await pipe.execute()
        except Exception:
            logger.exception("Redis setex failed; using fallback")
            for k, v in zip(keys, vectors):
                self._fallback[k] = v


# ── Front door ───────────────────────────────────────────────────────────


class EmbeddingService:
    """Cache-aware async embedder."""

    def __init__(
        self,
        provider: Optional[EmbeddingProvider] = None,
        cache: Optional[EmbeddingCache] = None,
    ) -> None:
        self._provider = provider or _build_provider(DEFAULT_PROVIDER)
        self._cache = cache or EmbeddingCache()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def dim(self) -> int:
        return self._provider.dim

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        # Cache lookups first.
        cached = await self._cache.get_many(self._provider.name, texts)
        missing_idx = [i for i, v in enumerate(cached) if v is None]
        if missing_idx:
            missing_texts = [texts[i] for i in missing_idx]
            fresh = await self._provider.embed(missing_texts)
            await self._cache.set_many(
                self._provider.name, missing_texts, fresh.vectors
            )
            for i, v in zip(missing_idx, fresh.vectors):
                cached[i] = v
        # All slots are now filled.
        return [v for v in cached if v is not None]

    def embed_sync(self, texts: Sequence[str]) -> List[List[float]]:
        return asyncio.run(self.embed(texts))


# ── Selection helper ─────────────────────────────────────────────────────


def _build_provider(name: str) -> EmbeddingProvider:
    name = (name or "hash").lower()
    if name == "voyage":
        return VoyageEmbeddingProvider()
    if name == "openai":
        return OpenAIEmbeddingProvider()
    return HashEmbeddingProvider()


_default_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    global _default_service
    if _default_service is None:
        _default_service = EmbeddingService()
    return _default_service


# ── Small vector utilities ───────────────────────────────────────────────


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors; 0 when either is zero-length."""
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


def vector_mean(vectors: Sequence[Sequence[float]]) -> List[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    return [x / len(vectors) for x in acc]
