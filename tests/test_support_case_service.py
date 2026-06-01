"""Tests for the SupportCase service: auto-attach, transitions, CSAT,
token issuance + verification.

Uses the project's in-memory SQLite fixture pattern (same as
``test_manager_anomaly_detector.py``).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
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
    customer = Customer(tenant_id=tenant.id, name="Acme Logistics")
    sync_session.add(customer)
    sync_session.commit()
    sync_session.refresh(tenant)
    sync_session.refresh(customer)
    return tenant, customer


def _make_interaction(sync_session, tenant, customer, *, domain="it_support"):
    from backend.app.models import Interaction

    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=customer.id if customer else None,
        channel="voice",
        domain=domain,
        title=f"Issue {uuid.uuid4().hex[:6]}",
    )
    sync_session.add(ix)
    sync_session.commit()
    return ix


# ── attach_or_create_case ──────────────────────────────────────────────


def test_attach_or_create_skips_non_support_domain(sync_session, seeded):
    from backend.app.services.support_case_service import attach_or_create_case

    tenant, customer = seeded
    ix = _make_interaction(sync_session, tenant, customer, domain="sales")
    result = attach_or_create_case(sync_session, ix)
    assert result is None
    assert ix.support_case_id is None


def test_attach_or_create_skips_when_no_customer(sync_session, seeded):
    from backend.app.services.support_case_service import attach_or_create_case

    tenant, _customer = seeded
    ix = _make_interaction(sync_session, tenant, None, domain="it_support")
    result = attach_or_create_case(sync_session, ix)
    assert result is None
    assert ix.support_case_id is None


def test_attach_or_create_opens_new_case_when_no_existing(sync_session, seeded):
    from backend.app.services.support_case_service import attach_or_create_case

    tenant, customer = seeded
    ix = _make_interaction(sync_session, tenant, customer)
    case = attach_or_create_case(sync_session, ix)
    assert case is not None
    assert case.status == "open"
    assert case.customer_id == customer.id
    assert ix.support_case_id == case.id


def test_attach_or_create_attaches_to_recent_open_case(sync_session, seeded):
    """Second interaction in the dedupe window attaches to the first
    interaction's case."""
    from backend.app.services.support_case_service import attach_or_create_case

    tenant, customer = seeded
    ix1 = _make_interaction(sync_session, tenant, customer)
    case1 = attach_or_create_case(sync_session, ix1)
    ix2 = _make_interaction(sync_session, tenant, customer)
    case2 = attach_or_create_case(sync_session, ix2)
    assert case2 is not None
    assert case2.id == case1.id
    assert ix2.support_case_id == case1.id


def test_attach_or_create_opens_new_case_when_existing_is_old(sync_session, seeded):
    """An older case past the dedupe window doesn't catch new interactions."""
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import (
        OPEN_WINDOW_HOURS,
        attach_or_create_case,
    )

    tenant, customer = seeded
    old = SupportCase(
        tenant_id=tenant.id,
        customer_id=customer.id,
        subject="old",
        status="open",
    )
    sync_session.add(old)
    sync_session.flush()
    old.opened_at = datetime.now(timezone.utc) - timedelta(
        hours=OPEN_WINDOW_HOURS + 1
    )
    sync_session.commit()

    ix = _make_interaction(sync_session, tenant, customer)
    case = attach_or_create_case(sync_session, ix)
    assert case is not None
    assert case.id != old.id


def test_attach_or_create_ignores_closed_cases(sync_session, seeded):
    """A recent but ``closed`` case does NOT get re-used."""
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import attach_or_create_case

    tenant, customer = seeded
    closed_case = SupportCase(
        tenant_id=tenant.id,
        customer_id=customer.id,
        subject="closed earlier today",
        status="closed",
    )
    sync_session.add(closed_case)
    sync_session.commit()
    ix = _make_interaction(sync_session, tenant, customer)
    case = attach_or_create_case(sync_session, ix)
    assert case is not None
    assert case.id != closed_case.id


# ── transition_status ──────────────────────────────────────────────────


def test_transition_stamps_escalated_at(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import transition_status

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id,
        customer_id=customer.id,
        subject="x",
    )
    sync_session.add(case)
    sync_session.commit()
    transition_status(sync_session, case, next_status="escalated")
    assert case.status == "escalated"
    assert case.escalated_at is not None


def test_transition_stamps_fcr_on_first_resolve(sync_session, seeded):
    """Resolving with exactly one linked interaction sets FCR=True."""
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import transition_status

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id,
        customer_id=customer.id,
        subject="x",
    )
    sync_session.add(case)
    sync_session.commit()
    ix = _make_interaction(sync_session, tenant, customer)
    ix.support_case_id = case.id
    sync_session.commit()
    transition_status(sync_session, case, next_status="resolved")
    assert case.first_contact_resolution is True
    assert case.resolved_at is not None


def test_transition_marks_fcr_false_when_multiple_touches(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import transition_status

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id,
        customer_id=customer.id,
        subject="x",
    )
    sync_session.add(case)
    sync_session.commit()
    for _ in range(2):
        ix = _make_interaction(sync_session, tenant, customer)
        ix.support_case_id = case.id
    sync_session.commit()
    transition_status(sync_session, case, next_status="resolved")
    assert case.first_contact_resolution is False


def test_transition_rejects_unknown_status(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import transition_status

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x"
    )
    sync_session.add(case)
    sync_session.commit()
    with pytest.raises(ValueError, match="Unknown"):
        transition_status(sync_session, case, next_status="zombie")


# ── record_csat ─────────────────────────────────────────────────────────


def test_record_csat_rejects_out_of_range(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import record_csat

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x", status="resolved"
    )
    sync_session.add(case)
    sync_session.commit()
    with pytest.raises(ValueError, match="1-5"):
        record_csat(sync_session, case, score=0)
    with pytest.raises(ValueError, match="1-5"):
        record_csat(sync_session, case, score=6)


def test_record_csat_rejects_while_open(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import record_csat

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x", status="open"
    )
    sync_session.add(case)
    sync_session.commit()
    with pytest.raises(ValueError, match="resolved"):
        record_csat(sync_session, case, score=5)


def test_record_csat_writes_on_resolved(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import record_csat

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x", status="resolved"
    )
    sync_session.add(case)
    sync_session.commit()
    record_csat(sync_session, case, score=4)
    sync_session.refresh(case)
    assert case.csat_score == 4


# ── CSAT token round-trip ──────────────────────────────────────────────


def test_csat_token_round_trip(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import (
        issue_csat_token,
        verify_csat_token,
    )

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x"
    )
    sync_session.add(case)
    sync_session.commit()
    token = issue_csat_token(case, secret="topsecret")
    verified = verify_csat_token(token, secret="topsecret")
    assert verified == case.id


def test_csat_token_rejects_wrong_secret(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import (
        issue_csat_token,
        verify_csat_token,
    )

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x"
    )
    sync_session.add(case)
    sync_session.commit()
    token = issue_csat_token(case, secret="real")
    assert verify_csat_token(token, secret="impersonator") is None


def test_csat_token_rejects_malformed_token():
    from backend.app.services.support_case_service import verify_csat_token

    assert verify_csat_token("no_dot_here", secret="x") is None
    assert verify_csat_token("not-a-uuid.signature", secret="x") is None
    assert verify_csat_token(f"{uuid.uuid4().hex}.short", secret="x") is None


def test_csat_token_rejects_tampered_case_id(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.support_case_service import (
        issue_csat_token,
        verify_csat_token,
    )

    tenant, customer = seeded
    case = SupportCase(
        tenant_id=tenant.id, customer_id=customer.id, subject="x"
    )
    sync_session.add(case)
    sync_session.commit()
    token = issue_csat_token(case, secret="x")
    _cid, sig = token.split(".", 1)
    bogus = f"{uuid.uuid4().hex}.{sig}"
    assert verify_csat_token(bogus, secret="x") is None
