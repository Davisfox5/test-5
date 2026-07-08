"""Tests for the deterministic broken-commitment detector.

No LLM, no embeddings — a commitment is "broken" purely by the clock
(``due_date`` passed, ``met_at`` still NULL). Covers the status flip,
the ManagerAlert write + dedup, and the cases that must NOT fire (met,
no due_date, not yet due).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

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


def _make_commitment(
    sync_session,
    tenant,
    cust,
    *,
    due_date=None,
    met_at=None,
    status="open",
    description="Send the signed MSA",
):
    from backend.app.models import CustomerCommitment

    row = CustomerCommitment(
        tenant_id=tenant.id,
        customer_id=cust.id,
        description=description,
        due_date=due_date,
        met_at=met_at,
        status=status,
    )
    sync_session.add(row)
    sync_session.commit()
    sync_session.refresh(row)
    return row


def test_flags_overdue_unmet_commitment(sync_session, seeded):
    from backend.app.models import ManagerAlert
    from backend.app.services.commitment_detector import detect_and_flag

    tenant, cust = seeded
    today = date(2026, 7, 8)
    commitment = _make_commitment(
        sync_session, tenant, cust, due_date=today - timedelta(days=5)
    )
    out = detect_and_flag(sync_session, tenant, today=today)
    assert out == {"scanned": 1, "flagged": 1}
    sync_session.refresh(commitment)
    assert commitment.status == "broken"
    alert = sync_session.execute(select(ManagerAlert)).scalar_one()
    assert alert.kind == "broken_commitment_detected"
    assert alert.evidence["days_overdue"] == 5
    assert "Northstar" in alert.title


def test_ignores_commitment_not_yet_due(sync_session, seeded):
    from backend.app.services.commitment_detector import detect_and_flag

    tenant, cust = seeded
    today = date(2026, 7, 8)
    _make_commitment(sync_session, tenant, cust, due_date=today + timedelta(days=3))
    out = detect_and_flag(sync_session, tenant, today=today)
    assert out == {"scanned": 0, "flagged": 0}


def test_ignores_commitment_with_no_due_date(sync_session, seeded):
    from backend.app.services.commitment_detector import detect_and_flag

    tenant, cust = seeded
    _make_commitment(sync_session, tenant, cust, due_date=None)
    out = detect_and_flag(sync_session, tenant, today=date(2026, 7, 8))
    assert out == {"scanned": 0, "flagged": 0}


def test_ignores_commitment_already_met(sync_session, seeded):
    from backend.app.services.commitment_detector import detect_and_flag

    tenant, cust = seeded
    today = date(2026, 7, 8)
    _make_commitment(
        sync_session,
        tenant,
        cust,
        due_date=today - timedelta(days=5),
        met_at=datetime.now(timezone.utc),
        status="met",
    )
    out = detect_and_flag(sync_session, tenant, today=today)
    assert out == {"scanned": 0, "flagged": 0}


def test_severity_escalates_past_two_weeks_overdue(sync_session, seeded):
    from backend.app.models import ManagerAlert
    from backend.app.services.commitment_detector import detect_and_flag

    tenant, cust = seeded
    today = date(2026, 7, 8)
    _make_commitment(sync_session, tenant, cust, due_date=today - timedelta(days=20))
    detect_and_flag(sync_session, tenant, today=today)
    alert = sync_session.execute(select(ManagerAlert)).scalar_one()
    assert alert.severity == "high"


def test_rerun_does_not_duplicate_alert(sync_session, seeded):
    from backend.app.models import ManagerAlert
    from backend.app.services.commitment_detector import detect_and_flag

    tenant, cust = seeded
    today = date(2026, 7, 8)
    _make_commitment(sync_session, tenant, cust, due_date=today - timedelta(days=5))
    detect_and_flag(sync_session, tenant, today=today)
    # Second scan the same day: commitment is now status='broken', so
    # the query no longer selects it at all.
    out = detect_and_flag(sync_session, tenant, today=today)
    assert out == {"scanned": 0, "flagged": 0}
    alerts = sync_session.execute(select(ManagerAlert)).scalars().all()
    assert len(alerts) == 1
