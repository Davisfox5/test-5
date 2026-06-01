"""Tests for the AI cross-customer support-trend detector.

The deterministic parts (clustering + emerging-trend rule + persist)
are covered here. The Voyage embedding pass is not tested — that's
an HTTP call to an external provider; the embedding column exists
and the rest of the pipeline runs over whatever vectors are present.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


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


def _make_case(
    sync_session,
    tenant,
    *,
    subject: str,
    embedding,
    when: datetime,
    customer_id=None,
):
    from backend.app.models import SupportCase

    case = SupportCase(
        tenant_id=tenant.id,
        customer_id=customer_id,
        subject=subject,
        subject_embedding=list(embedding),
    )
    sync_session.add(case)
    sync_session.flush()
    case.opened_at = when
    sync_session.commit()
    return case


def _unit(vec):
    """Normalize a vector to unit length so cosine-similarity math is
    crisp in tests (we author small synthetic vectors)."""
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else list(vec)


# ── Cosine similarity helper ─────────────────────────────────────────


def test_cosine_similarity_matches_expected():
    from backend.app.services.support_trend_detector import _cosine_similarity

    assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert _cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert _cosine_similarity([], [1]) == 0.0


# ── Clustering ────────────────────────────────────────────────────────


def test_cluster_cases_groups_similar(sync_session, seeded):
    from backend.app.services.support_trend_detector import (
        CLUSTER_SIM_THRESHOLD,
        cluster_cases,
    )

    now = datetime.now(timezone.utc)
    # 3 cases on the same vector → one cluster.
    v = _unit([1, 1, 0])
    a = _make_case(sync_session, seeded, subject="VPN reauth", embedding=v, when=now - timedelta(days=1))
    b = _make_case(sync_session, seeded, subject="VPN reauth", embedding=v, when=now - timedelta(days=2))
    c = _make_case(sync_session, seeded, subject="VPN reauth", embedding=v, when=now - timedelta(days=3))
    # 1 case on an orthogonal vector → second cluster.
    w = _unit([0, 0, 1])
    d = _make_case(sync_session, seeded, subject="printer offline", embedding=w, when=now - timedelta(days=1))
    clusters = cluster_cases([a, b, c, d])
    assert len(clusters) == 2
    sizes = sorted(len(cl.cases) for cl in clusters)
    assert sizes == [1, 3]
    # Threshold tuning safety: the test expects similarity 1.0 to clear
    # the threshold and 0.0 not to.
    assert CLUSTER_SIM_THRESHOLD <= 1.0


def test_cluster_skips_cases_without_embedding(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_trend_detector import cluster_cases

    case = SupportCase(
        tenant_id=seeded.id,
        subject="orphan",
        subject_embedding=None,
    )
    sync_session.add(case)
    sync_session.commit()
    out = cluster_cases([case])
    assert out == []


# ── Emerging-trend rule ──────────────────────────────────────────────


def test_find_emerging_trends_fires_on_growth(sync_session, seeded):
    """Recent window has 4 cases on the same theme; prior window has 1.
    Growth ratio 4.0 > 2.0 trigger."""
    from backend.app.services.support_trend_detector import (
        cluster_cases,
        find_emerging_trends,
    )

    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    recent_cases = [
        _make_case(
            sync_session,
            seeded,
            subject="VPN drops",
            embedding=v,
            when=now - timedelta(days=2 + i),
        )
        for i in range(4)
    ]
    prior_case = _make_case(
        sync_session,
        seeded,
        subject="VPN drops",
        embedding=v,
        when=now - timedelta(days=20),
    )
    clusters = cluster_cases(recent_cases + [prior_case])
    trends = find_emerging_trends(clusters, now=now)
    assert len(trends) == 1
    t = trends[0]
    assert t.recent_count == 4
    assert t.prior_count == 1
    assert t.growth_ratio == pytest.approx(4.0)
    assert 0.0 <= t.confidence <= 1.0


def test_find_emerging_trends_skips_stable_cluster(sync_session, seeded):
    """4 cases per window in BOTH windows — no growth → don't fire.

    Demonstrates the "stable cluster doesn't fire" promise: a
    persistent issue that the team is already handling at a steady
    rate isn't an emerging trend."""
    from backend.app.services.support_trend_detector import (
        cluster_cases,
        find_emerging_trends,
    )

    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    cases = []
    for i in range(4):
        cases.append(
            _make_case(
                sync_session,
                seeded,
                subject="recurring",
                embedding=v,
                when=now - timedelta(days=2 + i),
            )
        )
    for i in range(4):
        cases.append(
            _make_case(
                sync_session,
                seeded,
                subject="recurring",
                embedding=v,
                when=now - timedelta(days=18 + i),
            )
        )
    clusters = cluster_cases(cases)
    trends = find_emerging_trends(clusters, now=now)
    assert trends == []


def test_find_emerging_trends_fires_on_fresh_cluster(sync_session, seeded):
    """No prior-window cases at all + recent count at floor → fresh
    cluster, fires even though there's no "growth ratio" per se."""
    from backend.app.services.support_trend_detector import (
        cluster_cases,
        find_emerging_trends,
    )

    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    recent = [
        _make_case(
            sync_session,
            seeded,
            subject="brand new",
            embedding=v,
            when=now - timedelta(days=1 + i),
        )
        for i in range(3)
    ]
    clusters = cluster_cases(recent)
    trends = find_emerging_trends(clusters, now=now)
    assert len(trends) == 1


def test_find_emerging_trends_requires_min_cluster_size(sync_session, seeded):
    """Two cases on the same theme don't trip the floor."""
    from backend.app.services.support_trend_detector import (
        cluster_cases,
        find_emerging_trends,
    )

    v = _unit([1, 1, 0])
    now = datetime.now(timezone.utc)
    cases = [
        _make_case(
            sync_session,
            seeded,
            subject="same",
            embedding=v,
            when=now - timedelta(days=2 + i),
        )
        for i in range(2)
    ]
    clusters = cluster_cases(cases)
    trends = find_emerging_trends(clusters, now=now)
    assert trends == []


# ── Persist ──────────────────────────────────────────────────────────


def test_persist_writes_alert_and_recommendation(sync_session, seeded):
    from backend.app.models import ManagerAlert, ManagerRecommendation
    from backend.app.services.support_trend_detector import (
        EmergingTrend,
        persist_trends,
    )

    cluster_id = str(uuid.uuid4())
    t = EmergingTrend(
        cluster_id=cluster_id,
        recent_count=5,
        prior_count=1,
        growth_ratio=5.0,
        confidence=0.78,
        sample_subjects=["VPN reauth", "VPN drops"],
        sample_case_ids=[uuid.uuid4()],
        customer_count=4,
    )
    out = persist_trends(sync_session, seeded, [t])
    assert out == {"alerts_inserted": 1, "recs_inserted": 1}
    alerts = (
        sync_session.execute(select(ManagerAlert))
    ).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].kind == "recurring_issue_detected"
    assert alerts[0].severity == "high"  # confidence >= 0.7
    recs = (
        sync_session.execute(select(ManagerRecommendation))
    ).scalars().all()
    assert len(recs) == 1
    assert recs[0].category == "address_recurring_issue"


def test_persist_dedup_on_repeat_call(sync_session, seeded):
    from backend.app.models import ManagerAlert
    from backend.app.services.support_trend_detector import (
        EmergingTrend,
        persist_trends,
    )

    cluster_id = str(uuid.uuid4())
    t = EmergingTrend(
        cluster_id=cluster_id,
        recent_count=4,
        prior_count=1,
        growth_ratio=4.0,
        confidence=0.6,
        sample_subjects=["s"],
        sample_case_ids=[uuid.uuid4()],
        customer_count=1,
    )
    persist_trends(sync_session, seeded, [t])
    persist_trends(sync_session, seeded, [t])
    alerts = (
        sync_session.execute(select(ManagerAlert))
    ).scalars().all()
    assert len(alerts) == 1
