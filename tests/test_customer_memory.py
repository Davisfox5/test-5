"""Tests for the customer relationship memory extractor.

Covers ``update_from_interaction`` (the post-analysis hook), the
concern-status transition rules (positive sentiment -> monitoring,
negative -> active even when previously resolved), severity bumping,
evidence accumulation, and the per-customer commitment dedup.

The ``sweep_dormant_concerns`` background sweep is also covered —
that's the per-customer-and-window analog of the QBR job.
"""
from __future__ import annotations

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
    from backend.app.models import Customer, Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    cust = Customer(tenant_id=tenant.id, name="Northstar")
    sync_session.add(cust)
    sync_session.commit()
    sync_session.refresh(tenant)
    sync_session.refresh(cust)
    return tenant, cust


def _add_interaction(sync_session, tenant, cust, *, domain="sales", when=None):
    from backend.app.models import Interaction

    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=cust.id,
        channel="voice",
        domain=domain,
    )
    sync_session.add(ix)
    sync_session.flush()
    if when is not None:
        ix.created_at = when
    sync_session.commit()
    return ix


# ── update_from_interaction: concerns ──────────────────────────────────


def test_update_creates_concern_on_first_mention(sync_session, seeded):
    from backend.app.models import CustomerConcern
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix = _add_interaction(sync_session, tenant, cust)
    insights = {
        "concerns_raised": [
            {
                "topic": "Pricing",
                "description": "Customer pushed back on the per-seat tier.",
                "severity": "high",
                "sentiment": "negative",
                "quote": "It's way more than we budgeted.",
            }
        ]
    }
    counts = update_from_interaction(sync_session, ix, insights)
    assert counts["concerns"] == 1
    row = (
        sync_session.execute(
            select(CustomerConcern).where(
                CustomerConcern.customer_id == cust.id
            )
        )
    ).scalar_one()
    assert row.topic == "pricing"  # normalized
    assert row.status == "active"
    assert row.severity == "high"
    assert row.source_motion == "sales"
    assert len(row.evidence) == 1


def test_update_upserts_existing_concern_and_appends_evidence(
    sync_session, seeded
):
    from backend.app.models import CustomerConcern
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix1 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix1,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "negative"}]},
    )
    ix2 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix2,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "negative"}]},
    )
    rows = (
        sync_session.execute(select(CustomerConcern))
    ).scalars().all()
    assert len(rows) == 1
    assert len(rows[0].evidence) == 2


def test_positive_sentiment_moves_active_to_monitoring(sync_session, seeded):
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix1 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix1,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "negative"}]},
    )
    ix2 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix2,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "positive"}]},
    )
    from backend.app.models import CustomerConcern

    row = (
        sync_session.execute(select(CustomerConcern))
    ).scalar_one()
    assert row.status == "monitoring"


def test_negative_sentiment_reopens_resolved_concern(sync_session, seeded):
    from backend.app.models import CustomerConcern
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix1 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix1,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "negative"}]},
    )
    row = (sync_session.execute(select(CustomerConcern))).scalar_one()
    row.status = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    sync_session.commit()

    ix2 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix2,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "negative"}]},
    )
    sync_session.refresh(row)
    assert row.status == "active"
    assert row.resolved_at is None


def test_severity_only_bumps_up(sync_session, seeded):
    from backend.app.models import CustomerConcern
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix1 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix1,
        {
            "concerns_raised": [
                {"topic": "pricing", "severity": "high", "sentiment": "negative"}
            ]
        },
    )
    ix2 = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix2,
        {
            "concerns_raised": [
                {"topic": "pricing", "severity": "low", "sentiment": "negative"}
            ]
        },
    )
    row = (sync_session.execute(select(CustomerConcern))).scalar_one()
    assert row.severity == "high"


def test_short_circuits_without_customer(sync_session, seeded):
    from backend.app.models import Interaction
    from backend.app.services.customer_memory import update_from_interaction

    tenant, _cust = seeded
    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=None,  # orphan
        channel="voice",
        domain="sales",
    )
    sync_session.add(ix)
    sync_session.commit()
    counts = update_from_interaction(
        sync_session,
        ix,
        {"concerns_raised": [{"topic": "pricing", "sentiment": "negative"}]},
    )
    assert counts == {"concerns": 0, "commitments": 0}


# ── update_from_interaction: customer commitments ──────────────────────


def test_customer_commitment_inserts_once(sync_session, seeded):
    from backend.app.models import CustomerCommitment
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix = _add_interaction(sync_session, tenant, cust)
    body = {
        "customer_commitments": [
            {
                "description": "Send the signed MSA by Friday",
                "quote": "We'll send the MSA by Friday.",
                "due_date": "2026-06-12",
            }
        ]
    }
    update_from_interaction(sync_session, ix, body)
    update_from_interaction(sync_session, ix, body)  # same again — dedup
    rows = (sync_session.execute(select(CustomerCommitment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].due_date is not None


def test_customer_commitment_handles_missing_due_date(sync_session, seeded):
    from backend.app.models import CustomerCommitment
    from backend.app.services.customer_memory import update_from_interaction

    tenant, cust = seeded
    ix = _add_interaction(sync_session, tenant, cust)
    update_from_interaction(
        sync_session,
        ix,
        {"customer_commitments": [{"description": "Loop in legal."}]},
    )
    row = (sync_session.execute(select(CustomerCommitment))).scalar_one()
    assert row.due_date is None
    assert row.status == "open"


# ── sweep_dormant_concerns ────────────────────────────────────────────


def test_sweep_moves_stale_active_to_dormant(sync_session, seeded):
    from backend.app.models import CustomerConcern
    from backend.app.services.customer_memory import (
        DORMANT_AFTER_DAYS,
        sweep_dormant_concerns,
    )

    tenant, cust = seeded
    row = CustomerConcern(
        tenant_id=tenant.id,
        customer_id=cust.id,
        topic="pricing",
        status="active",
        severity="medium",
    )
    sync_session.add(row)
    sync_session.flush()
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(
        days=DORMANT_AFTER_DAYS + 1
    )
    sync_session.commit()
    transitioned = sweep_dormant_concerns(sync_session)
    assert transitioned == 1
    sync_session.refresh(row)
    assert row.status == "dormant"


def test_sweep_leaves_fresh_concerns_alone(sync_session, seeded):
    from backend.app.models import CustomerConcern
    from backend.app.services.customer_memory import sweep_dormant_concerns

    tenant, cust = seeded
    row = CustomerConcern(
        tenant_id=tenant.id,
        customer_id=cust.id,
        topic="pricing",
        status="active",
        severity="medium",
    )
    sync_session.add(row)
    sync_session.commit()
    assert sweep_dormant_concerns(sync_session) == 0
    sync_session.refresh(row)
    assert row.status == "active"
