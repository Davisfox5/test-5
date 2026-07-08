"""Tests for the Sales-domain trend caller (generalized via trend_engine).

Uses the same deterministic fake embedder pattern as
``test_concern_aggregation.py`` — identical representative text clusters
together, different text doesn't — so the growth/confidence rule is
exercised end to end without a real Voyage/Redis round trip.
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


def _make_interaction(
    sync_session, tenant, cust, *, domain, insights, days_ago
):
    from backend.app.models import Interaction

    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=cust.id,
        channel="voice",
        domain=domain,
        insights=insights,
    )
    sync_session.add(ix)
    sync_session.flush()
    ix.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    sync_session.commit()
    return ix


@pytest.mark.asyncio
async def test_fires_on_converging_competitor_objection(sync_session, seeded):
    from backend.app.models import ManagerAlert, ManagerRecommendation
    from backend.app.services.sales_trend_detector import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_interaction(
            sync_session,
            seeded,
            cust,
            domain="sales",
            insights={"competitor_mentions": [{"name": "Rival Corp"}]},
            days_ago=1 + i,
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out["trends_found"] == 1
    assert out["alerts_inserted"] == 1
    assert out["recs_inserted"] == 1

    alert = sync_session.execute(select(ManagerAlert)).scalar_one()
    assert alert.kind == "sales_trend_detected"
    assert alert.domain == "sales"

    rec = sync_session.execute(select(ManagerRecommendation)).scalar_one()
    assert rec.category == "address_sales_trend"
    assert rec.domain == "sales"


@pytest.mark.asyncio
async def test_no_interactions_is_a_noop(sync_session, seeded):
    from backend.app.services.sales_trend_detector import run_for_tenant

    out = await run_for_tenant(sync_session, seeded)
    assert out == {
        "clusters": 0,
        "trends_found": 0,
        "alerts_inserted": 0,
        "recs_inserted": 0,
    }


@pytest.mark.asyncio
async def test_cs_domain_interactions_are_not_pulled_into_sales_scan(
    sync_session, seeded
):
    from backend.app.services.sales_trend_detector import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_interaction(
            sync_session,
            seeded,
            cust,
            domain="customer_service",
            insights={"competitor_mentions": [{"name": "Rival Corp"}]},
            days_ago=1 + i,
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out == {
        "clusters": 0,
        "trends_found": 0,
        "alerts_inserted": 0,
        "recs_inserted": 0,
    }


@pytest.mark.asyncio
async def test_interactions_with_no_extractable_text_are_skipped(
    sync_session, seeded
):
    from backend.app.services.sales_trend_detector import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_interaction(
            sync_session, seeded, cust, domain="sales", insights={}, days_ago=1 + i
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out["clusters"] == 0
    assert out["alerts_inserted"] == 0
