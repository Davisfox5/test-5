"""Tests for the CS-domain trend caller (generalized via trend_engine).

Mirrors ``test_sales_trend_detector.py`` — same deterministic fake
embedder, same growth-detection exercise — just over the
``customer_service`` domain.
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


def _make_interaction(sync_session, tenant, cust, *, domain, insights, days_ago):
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
async def test_fires_on_converging_product_feedback(sync_session, seeded):
    from backend.app.models import ManagerAlert, ManagerRecommendation
    from backend.app.services.cs_trend_detector import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_interaction(
            sync_session,
            seeded,
            cust,
            domain="customer_service",
            insights={"product_feedback": ["Export to CSV is broken"]},
            days_ago=1 + i,
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out["trends_found"] == 1
    assert out["alerts_inserted"] == 1
    assert out["recs_inserted"] == 1

    alert = sync_session.execute(select(ManagerAlert)).scalar_one()
    assert alert.kind == "cs_trend_detected"
    assert alert.domain == "customer_service"

    rec = sync_session.execute(select(ManagerRecommendation)).scalar_one()
    assert rec.category == "address_cs_trend"
    assert rec.domain == "customer_service"


@pytest.mark.asyncio
async def test_no_interactions_is_a_noop(sync_session, seeded):
    from backend.app.services.cs_trend_detector import run_for_tenant

    out = await run_for_tenant(sync_session, seeded)
    assert out == {
        "clusters": 0,
        "trends_found": 0,
        "alerts_inserted": 0,
        "recs_inserted": 0,
    }


@pytest.mark.asyncio
async def test_sales_domain_interactions_are_not_pulled_into_cs_scan(
    sync_session, seeded
):
    from backend.app.services.cs_trend_detector import run_for_tenant

    for i, name in enumerate(["Acme", "Bolt", "Cinder"]):
        cust = _make_customer(sync_session, seeded, name)
        _make_interaction(
            sync_session,
            seeded,
            cust,
            domain="sales",
            insights={"product_feedback": ["Export to CSV is broken"]},
            days_ago=1 + i,
        )

    out = await run_for_tenant(sync_session, seeded)
    assert out["alerts_inserted"] == 0
