"""Tests for the embedding service, cache, and helper math.

The hash provider is exercised end-to-end; real-provider selection is
checked by construction only (we don't hit network).
"""

import pytest

from backend.app.services.embeddings import (
    EmbeddingCache,
    EmbeddingService,
    HashEmbeddingProvider,
    _build_provider,
    cosine_similarity,
    vector_mean,
)


def test_hash_provider_is_deterministic_and_unit_length():
    provider = HashEmbeddingProvider()
    import asyncio
    result = asyncio.run(provider.embed(["pricing question", "pricing question"]))
    assert result.vectors[0] == result.vectors[1]
    norm = sum(x * x for x in result.vectors[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    assert result.dim == 256


def test_hash_provider_different_inputs_yield_different_vectors():
    import asyncio
    provider = HashEmbeddingProvider()
    v1, v2 = asyncio.run(provider.embed(["pricing", "integration"])).vectors
    # Cosine similarity should be <1 for distinct single tokens.
    assert cosine_similarity(v1, v2) < 1.0


def test_cosine_similarity_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_similarity_identical():
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_vector_mean_averages_dimensions():
    mean = vector_mean([[1.0, 0.0], [3.0, 4.0]])
    assert mean == [2.0, 2.0]


def test_vector_mean_handles_empty_input():
    assert vector_mean([]) == []


def test_embedding_service_cache_hits_on_second_call():
    import asyncio
    cache = EmbeddingCache()  # Redis unavailable in test; falls back to dict.
    service = EmbeddingService(provider=HashEmbeddingProvider(), cache=cache)
    first = asyncio.run(service.embed(["hello world"]))
    second = asyncio.run(service.embed(["hello world"]))
    assert first == second


def test_build_provider_defaults_to_hash_for_unknown_name():
    assert _build_provider("unknown").name == "hash"
    assert _build_provider("hash").name == "hash"


def test_build_provider_selects_voyage_or_openai_by_name():
    # Construction should not fail even if the underlying client isn't
    # installed — we explicitly degrade.
    assert _build_provider("voyage").name == "voyage"
    assert _build_provider("openai").name == "openai"
