"""Tests for cross-customer concern aggregation (``concern_aggregation.py``).

Uses a deterministic fake embedder (monkeypatched onto
``trend_engine.build_cached_embedder``) so the clustering behavior is
testable without a real Voyage/Redis round trip — the embedding pass
itself is out of scope here, same convention as
``tests/test_support_trend_detector.py``.
"""
from __future__ import annotations

import hashlib
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


class _FakeEmbedder:
    """Identical text -> identical vector; different text -> a different
    (near-orthogonal) one. Deterministic, no network."""

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


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    from backend.app.services import trend_engine

    monkeypatch.setattr(trend_engine, "build_cached_embedder", lambda: _FakeEmbedder())


def _make_customer(sync_session, tenant, name):
    from backend.app.models import Customer

    cust = Customer(tenant_id=tenant.id, name=name)
    sync_session.add(cust)
    sync_session.commit()
    sync_session.refresh(cust)
    return cust


def _make_concern(
    sync_session,
    tenant,
    cust,
    *,
    topic="pricing",
    status="active",
    first_seen_days_ago=1,
    evidence=None,
):
    from backend.app.models import CustomerConcern

    row = CustomerConcern(
        tenant_id=tenant.id,
        customer_id=cust.id,
        topic=topic,
        status=status,
        severity="medium",
        evidence=evidence or [],
    )
    sync_session.add(row)
    sync_session.flush()
    row.first_seen_at = datetime.now(timezone.utc) - timedelta(days=first_seen_days_ago)
    row.last_seen_at = row.first_seen_at
    sync_session.commit()
    return row


@pytest.mark.asyncio
async def test_fires_when_several_customers_raise_same_concern(sync_session, seeded):
    from backend.app.models import ManagerAlert
    from backend.app.services.concern_aggregation import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_concern(sync_session, seeded, cust, topic="pricing", first_seen_days_ago=1 + i)

    out = await run_for_tenant(sync_session, seeded)
    assert out["trends_found"] == 1
    assert out["alerts_inserted"] == 1
    alerts = sync_session.execute(select(ManagerAlert)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].kind == "customer_concern_trend_detected"
    assert alerts[0].domain == "customer_service"
    assert "several customers" in alerts[0].title.lower() or "raised" in alerts[0].title.lower()


@pytest.mark.asyncio
async def test_single_customer_repeating_topic_does_not_fire(sync_session, seeded):
    """One customer's own concern, however tracked, isn't 'several
    customers' — the cross-customer floor requires >= 2 distinct
    customers even if >= MIN_CLUSTER_SIZE rows cluster together."""
    from backend.app.services.concern_aggregation import run_for_tenant

    cust = _make_customer(sync_session, seeded, "Solo")
    _make_concern(sync_session, seeded, cust, topic="pricing", first_seen_days_ago=1)

    out = await run_for_tenant(sync_session, seeded)
    assert out["alerts_inserted"] == 0


@pytest.mark.asyncio
async def test_only_active_concerns_count(sync_session, seeded):
    from backend.app.services.concern_aggregation import run_for_tenant

    for i, (name, status) in enumerate(
        [("Acme", "resolved"), ("Bolt", "monitoring"), ("Cinder", "dormant")]
    ):
        cust = _make_customer(sync_session, seeded, name)
        _make_concern(
            sync_session, seeded, cust, topic="pricing", status=status,
            first_seen_days_ago=1 + i,
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out == {"clusters": 0, "trends_found": 0, "alerts_inserted": 0}


@pytest.mark.asyncio
async def test_severity_weight_from_valence_feeds_total_weight(sync_session, seeded):
    """Low valence (very negative) mentions weight higher than absent
    ones — total_weight isn't surfaced to the caller directly here, but
    the alert body should include a severity reading built from it."""
    from backend.app.models import ManagerAlert
    from backend.app.services.concern_aggregation import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_concern(
            sync_session,
            seeded,
            cust,
            topic="pricing",
            first_seen_days_ago=1 + i,
            evidence=[{"valence": 1.0}],  # very negative -> weight 9.0
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out["alerts_inserted"] == 1
    alert = sync_session.execute(select(ManagerAlert)).scalar_one()
    assert "severity" in alert.body.lower()
    # Plain-language guard: no internal jargon leaking into user copy.
    for banned in ("valence", "embedding", "cluster", "cohort", "confidence"):
        assert banned not in alert.title.lower()
        assert banned not in alert.body.lower()
