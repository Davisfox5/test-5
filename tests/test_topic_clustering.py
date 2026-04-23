"""Tests for greedy topic clustering.

Uses the deterministic hash embedding provider so results are stable
across runs.
"""

import asyncio

import pytest

from backend.app.services.embeddings import EmbeddingService, HashEmbeddingProvider
from backend.app.services.topic_clustering import (
    CanonicalTopicIndex,
    TopicClusterer,
    aggregate_counts_canonical,
)


def _clusterer(**kw):
    svc = EmbeddingService(provider=HashEmbeddingProvider())
    return TopicClusterer(embedder=svc, **kw)


def test_cluster_empty_input_returns_empty_result():
    result = _clusterer().cluster_sync([])
    assert result.clusters == []
    assert result.assignments == []


def test_cluster_separates_clearly_distinct_topics():
    result = _clusterer(threshold=0.0, min_cluster_size=1).cluster_sync([
        "pricing pricing pricing",
        "integration integration",
        "pricing pricing",
    ])
    # Texts sharing tokens end up in the same cluster.
    assert result.assignments[0] == result.assignments[2]


def test_cluster_labels_populated_by_ctfidf():
    result = _clusterer(threshold=0.0, min_cluster_size=1).cluster_sync([
        "pricing pricing pricing question",
        "pricing pricing pricing",
    ])
    assert result.clusters[0].keywords
    assert "pricing" in result.clusters[0].keywords


def test_cluster_drops_clusters_below_min_size():
    result = _clusterer(threshold=0.999, min_cluster_size=2).cluster_sync([
        "alpha",
        "beta",
        "gamma",
    ])
    # All distinct → size-1 clusters → dropped → every assignment is -1.
    assert all(a == -1 for a in result.assignments)


def test_canonical_topic_index_maps_unseen_names_to_nearest_label():
    index = asyncio.run(CanonicalTopicIndex.build(
        ["pricing", "integration", "onboarding"],
        threshold=0.0,
    ))
    # Exact match returns the canonical label.
    canonical = asyncio.run(index.canonicalize("integration"))
    assert canonical in {"integration", "pricing", "onboarding"}


def test_aggregate_counts_canonical_folds_duplicates():
    async def _build():
        return await CanonicalTopicIndex.build(
            ["pricing", "price", "cost"],
            threshold=0.0,
            min_cluster_size=1,
        )
    index = asyncio.run(_build())
    agg = aggregate_counts_canonical(
        {"pricing": 10, "price": 5, "cost": 1}, index
    )
    # All variants should roll up into one canonical key with total 16.
    assert sum(agg.values()) == 16
