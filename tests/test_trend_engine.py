"""Tests for the domain-agnostic trend engine (``trend_engine.py``).

Covers the generic clustering + growth-detection + confidence rule on
synthetic corpora (mirrors ``tests/test_support_trend_detector.py`` but
over plain ``TrendItem`` rows instead of ``SupportCase``), plus the two
shared helpers new domain callers use: ``cluster_corpus`` (embed+cluster
in one call) and ``persist_alerts`` (shared ManagerAlert dedup).
"""
from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from backend.app.services import trend_engine
from backend.app.services.trend_engine import Cluster, TrendItem


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def sync_session():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded(sync_session):
    from backend.app.models import Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    sync_session.refresh(tenant)
    return tenant


def _unit(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else list(vec)


def _item(text, embedding, when, customer_id=None, weight=1.0, source_id=None):
    return TrendItem(
        source_id=source_id or uuid.uuid4(),
        text=text,
        timestamp=when,
        customer_id=customer_id,
        embedding=embedding,
        weight=weight,
    )


# ── cosine similarity ─────────────────────────────────────────────────


def test_cosine_similarity_matches_expected():
    assert trend_engine.cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert trend_engine.cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert trend_engine.cosine_similarity([], [1]) == 0.0


# ── cluster_items ───────────────────────────────────────────────────────


def test_cluster_items_groups_similar():
    now = datetime.now(timezone.utc)
    v = _unit([1, 1, 0])
    w = _unit([0, 0, 1])
    items = [
        _item("VPN reauth", v, now - timedelta(days=1)),
        _item("VPN reauth", v, now - timedelta(days=2)),
        _item("VPN reauth", v, now - timedelta(days=3)),
        _item("printer offline", w, now - timedelta(days=1)),
    ]
    clusters = trend_engine.cluster_items(items)
    assert len(clusters) == 2
    sizes = sorted(len(cl.items) for cl in clusters)
    assert sizes == [1, 3]


def test_cluster_items_skips_missing_embedding():
    item = _item("orphan", None, datetime.now(timezone.utc))
    assert trend_engine.cluster_items([item]) == []
    item2 = _item("orphan2", [], datetime.now(timezone.utc))
    assert trend_engine.cluster_items([item2]) == []


def test_cluster_items_respects_custom_threshold():
    """A looser threshold merges items a stricter one would split."""
    now = datetime.now(timezone.utc)
    a = _unit([1, 0])
    b = _unit([1, 0.3])  # cos sim ~0.958
    items = [_item("a", a, now), _item("b", b, now - timedelta(days=1))]
    strict = trend_engine.cluster_items(items, threshold=0.99)
    assert len(strict) == 2
    loose = trend_engine.cluster_items(items, threshold=0.9)
    assert len(loose) == 1


# ── find_emerging_trends ─────────────────────────────────────────────


def test_find_emerging_trends_fires_on_growth():
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    recent_items = [
        _item("VPN drops", v, now - timedelta(days=2 + i)) for i in range(4)
    ]
    prior_item = _item("VPN drops", v, now - timedelta(days=20))
    clusters = trend_engine.cluster_items(recent_items + [prior_item])
    trends = trend_engine.find_emerging_trends(clusters, now=now)
    assert len(trends) == 1
    t = trends[0]
    assert t.recent_count == 4
    assert t.prior_count == 1
    assert t.growth_ratio == pytest.approx(4.0)
    assert 0.0 <= t.confidence <= 1.0


def test_find_emerging_trends_skips_stable_cluster():
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    items = [_item("recurring", v, now - timedelta(days=2 + i)) for i in range(4)]
    items += [_item("recurring", v, now - timedelta(days=18 + i)) for i in range(4)]
    clusters = trend_engine.cluster_items(items)
    trends = trend_engine.find_emerging_trends(clusters, now=now)
    assert trends == []


def test_find_emerging_trends_fires_on_fresh_cluster():
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    items = [_item("brand new", v, now - timedelta(days=1 + i)) for i in range(3)]
    clusters = trend_engine.cluster_items(items)
    trends = trend_engine.find_emerging_trends(clusters, now=now)
    assert len(trends) == 1


def test_find_emerging_trends_requires_min_cluster_size():
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    items = [_item("same", v, now - timedelta(days=2 + i)) for i in range(2)]
    clusters = trend_engine.cluster_items(items)
    trends = trend_engine.find_emerging_trends(clusters, now=now)
    assert trends == []


def test_find_emerging_trends_honors_custom_min_cluster_size():
    """A caller can lower the floor (e.g. a smaller tenant corpus)."""
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    items = [_item("same", v, now - timedelta(days=2 + i)) for i in range(2)]
    clusters = trend_engine.cluster_items(items)
    trends = trend_engine.find_emerging_trends(clusters, now=now, min_cluster_size=2)
    assert len(trends) == 1


def test_find_emerging_trends_honors_custom_growth_ratio():
    """A looser growth ratio fires on a smaller jump than the 2.0 default."""
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    recent = [_item("x", v, now - timedelta(days=2 + i)) for i in range(3)]
    prior = [_item("x", v, now - timedelta(days=20))] * 2
    clusters = trend_engine.cluster_items(recent + prior)
    default_trends = trend_engine.find_emerging_trends(clusters, now=now)
    assert default_trends == []  # 3/2 = 1.5 < default 2.0
    loose_trends = trend_engine.find_emerging_trends(
        clusters, now=now, growth_ratio=1.2
    )
    assert len(loose_trends) == 1


def test_total_weight_sums_recent_items_weight():
    """A caller-supplied per-item weight (e.g. severity) accumulates on
    the resulting trend for the recent window only."""
    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    recent = [
        _item("x", v, now - timedelta(days=2 + i), weight=3.0) for i in range(3)
    ]
    stale = [_item("x", v, now - timedelta(days=40), weight=99.0)]
    clusters = trend_engine.cluster_items(recent + stale)
    trends = trend_engine.find_emerging_trends(clusters, now=now)
    assert len(trends) == 1
    assert trends[0].total_weight == pytest.approx(9.0)


# ── cluster_corpus (async embed + cluster) ───────────────────────────


class _FakeEmbedder:
    """Deterministic stand-in for the Redis-cached Voyage embedder:
    identical text -> identical (parallel) vector, different text ->
    (almost certainly) a different, non-parallel vector."""

    DIM = 16

    async def embed(self, texts):
        return [self._vec(t) for t in texts]

    @classmethod
    def _vec(cls, text):
        h = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
        idx = h % cls.DIM
        vec = [0.0] * cls.DIM
        vec[idx] = 1.0
        return vec


@pytest.mark.asyncio
async def test_cluster_corpus_embeds_and_clusters():
    now = datetime.now(timezone.utc)
    rows = [
        {"text": "pricing too high", "when": now - timedelta(days=1), "cust": "a"},
        {"text": "pricing too high", "when": now - timedelta(days=2), "cust": "b"},
        {"text": "pricing too high", "when": now - timedelta(days=3), "cust": "c"},
        {"text": "totally unrelated", "when": now - timedelta(days=1), "cust": "d"},
        {"text": None, "when": now, "cust": "e"},  # skipped: no text
    ]
    clusters = await trend_engine.cluster_corpus(
        rows,
        text_fn=lambda r: r["text"],
        timestamp_fn=lambda r: r["when"],
        customer_id_fn=lambda r: r["cust"],
        source_id_fn=lambda r: r["cust"],
        embedder=_FakeEmbedder(),
    )
    sizes = sorted(len(cl.items) for cl in clusters)
    assert sizes == [1, 3]


@pytest.mark.asyncio
async def test_cluster_corpus_skips_rows_with_no_text():
    rows = [{"text": None}, {"text": ""}]
    clusters = await trend_engine.cluster_corpus(
        rows,
        text_fn=lambda r: r["text"],
        timestamp_fn=lambda r: datetime.now(timezone.utc),
        customer_id_fn=lambda r: None,
        source_id_fn=lambda r: id(r),
        embedder=_FakeEmbedder(),
    )
    assert clusters == []


# ── persist_alerts ────────────────────────────────────────────────────


def test_persist_alerts_inserts_and_dedups(sync_session, seeded):
    from backend.app.models import ManagerAlert

    trend = trend_engine.EmergingTrend(
        cluster_id=str(uuid.uuid4()),
        recent_count=5,
        prior_count=1,
        growth_ratio=5.0,
        confidence=0.8,
        sample_texts=["pricing too high"],
        sample_ids=[uuid.uuid4()],
        customer_count=3,
    )
    inserted = trend_engine.persist_alerts(
        sync_session,
        seeded.id,
        [trend],
        kind="test_trend_detected",
        domain="sales",
        title_fn=lambda t: f"Trend: {t.sample_texts[0]}",
        body_fn=lambda t: f"{t.recent_count} mentions",
    )
    assert inserted == 1
    sync_session.commit()
    alerts = sync_session.execute(select(ManagerAlert)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].kind == "test_trend_detected"
    assert alerts[0].severity == "high"  # confidence >= 0.7
    assert alerts[0].domain == "sales"

    # Re-running with the same cluster_id doesn't duplicate.
    inserted_again = trend_engine.persist_alerts(
        sync_session,
        seeded.id,
        [trend],
        kind="test_trend_detected",
        domain="sales",
        title_fn=lambda t: f"Trend: {t.sample_texts[0]}",
        body_fn=lambda t: f"{t.recent_count} mentions",
    )
    assert inserted_again == 0
    sync_session.commit()
    alerts = sync_session.execute(select(ManagerAlert)).scalars().all()
    assert len(alerts) == 1
