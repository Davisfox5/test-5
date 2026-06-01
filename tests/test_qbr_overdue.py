"""Tests for the QBR-overdue detector + dedup helper.

The Celery task itself is thin glue around the two helpers; testing
the helpers + their interaction is the high-value coverage.
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
    from backend.app.models import Customer, Tenant, User

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    csm = User(
        tenant_id=tenant.id,
        email=f"csm-{uuid.uuid4().hex[:6]}@acme.test",
        role="agent",
        agent_domains=["customer_service"],
    )
    sync_session.add(csm)
    sync_session.flush()
    return tenant, csm


def _make_customer(
    sync_session,
    tenant,
    csm,
    *,
    name="Acct",
    onboarding="completed",
    owner=True,
):
    from backend.app.models import Customer

    cust = Customer(
        tenant_id=tenant.id,
        name=name,
        onboarding_status=onboarding,
        strongest_connection_user_id=csm.id if owner else None,
    )
    sync_session.add(cust)
    sync_session.commit()
    sync_session.refresh(cust)
    return cust


def _add_cs(sync_session, tenant, cust, *, when):
    from backend.app.models import Interaction

    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=cust.id,
        channel="voice",
        domain="customer_service",
    )
    sync_session.add(ix)
    sync_session.flush()
    ix.created_at = when
    sync_session.commit()
    return ix


# ── find_qbr_overdue_customers ─────────────────────────────────────────


def test_completed_onboarding_no_cs_ever_is_overdue(sync_session, seeded):
    from backend.app.services.cs_account_health import find_qbr_overdue_customers

    tenant, csm = seeded
    cust = _make_customer(sync_session, tenant, csm, name="Northstar")
    out = find_qbr_overdue_customers(sync_session, tenant.id)
    assert any(c.id == cust.id for c in out)


def test_completed_onboarding_recent_cs_is_not_overdue(sync_session, seeded):
    from backend.app.services.cs_account_health import find_qbr_overdue_customers

    tenant, csm = seeded
    cust = _make_customer(sync_session, tenant, csm, name="Polaris")
    _add_cs(sync_session, tenant, cust, when=datetime.now(timezone.utc) - timedelta(days=10))
    out = find_qbr_overdue_customers(sync_session, tenant.id)
    assert not any(c.id == cust.id for c in out)


def test_completed_onboarding_old_cs_is_overdue(sync_session, seeded):
    from backend.app.services.cs_account_health import find_qbr_overdue_customers

    tenant, csm = seeded
    cust = _make_customer(sync_session, tenant, csm, name="Vega")
    _add_cs(sync_session, tenant, cust, when=datetime.now(timezone.utc) - timedelta(days=120))
    out = find_qbr_overdue_customers(sync_session, tenant.id)
    assert any(c.id == cust.id for c in out)


def test_in_progress_onboarding_never_overdue(sync_session, seeded):
    """A customer still onboarding isn't on the QBR clock yet — they
    have a different motion. The CS-engagement-cadence detector
    shouldn't ping the CSM."""
    from backend.app.services.cs_account_health import find_qbr_overdue_customers

    tenant, csm = seeded
    cust = _make_customer(
        sync_session, tenant, csm, name="Onboarding", onboarding="in_progress"
    )
    out = find_qbr_overdue_customers(sync_session, tenant.id)
    assert not any(c.id == cust.id for c in out)


def test_no_owner_is_skipped(sync_session, seeded):
    """Without a ``strongest_connection_user_id`` there's no one to
    notify — skip the candidate rather than ping the wrong CSM."""
    from backend.app.services.cs_account_health import find_qbr_overdue_customers

    tenant, csm = seeded
    cust = _make_customer(sync_session, tenant, csm, name="Orphan", owner=False)
    out = find_qbr_overdue_customers(sync_session, tenant.id)
    assert not any(c.id == cust.id for c in out)


# ── Dedup helper ───────────────────────────────────────────────────────


def test_dedup_blocks_fresh_repeat(sync_session, seeded):
    from backend.app.models import Notification
    from backend.app.services.cs_account_health import should_fire_qbr_overdue

    tenant, csm = seeded
    cust = _make_customer(sync_session, tenant, csm, name="Fresh")
    n = Notification(
        tenant_id=tenant.id,
        user_id=csm.id,
        kind="qbr_overdue",
        title="QBR overdue: Fresh",
        link_url=f"/cs/accounts/{cust.id}",
    )
    sync_session.add(n)
    sync_session.commit()
    assert should_fire_qbr_overdue(sync_session, cust) is False


def test_dedup_passes_after_window(sync_session, seeded):
    from backend.app.models import Notification
    from backend.app.services.cs_account_health import (
        QBR_NOTIF_DEDUP_DAYS,
        should_fire_qbr_overdue,
    )

    tenant, csm = seeded
    cust = _make_customer(sync_session, tenant, csm, name="OldPing")
    n = Notification(
        tenant_id=tenant.id,
        user_id=csm.id,
        kind="qbr_overdue",
        title="QBR overdue: OldPing",
        link_url=f"/cs/accounts/{cust.id}",
    )
    sync_session.add(n)
    sync_session.flush()
    n.created_at = datetime.now(timezone.utc) - timedelta(
        days=QBR_NOTIF_DEDUP_DAYS + 1
    )
    sync_session.commit()
    assert should_fire_qbr_overdue(sync_session, cust) is True


def test_dedup_is_per_customer(sync_session, seeded):
    from backend.app.models import Notification
    from backend.app.services.cs_account_health import should_fire_qbr_overdue

    tenant, csm = seeded
    a = _make_customer(sync_session, tenant, csm, name="A")
    b = _make_customer(sync_session, tenant, csm, name="B")
    # A has a fresh notification; B should still fire.
    n = Notification(
        tenant_id=tenant.id,
        user_id=csm.id,
        kind="qbr_overdue",
        title="QBR overdue: A",
        link_url=f"/cs/accounts/{a.id}",
    )
    sync_session.add(n)
    sync_session.commit()
    assert should_fire_qbr_overdue(sync_session, a) is False
    assert should_fire_qbr_overdue(sync_session, b) is True
