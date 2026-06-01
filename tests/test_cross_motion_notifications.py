"""Tests for the cross-motion notification kinds added in PR
``cross-motion-notifications``: ``case_assigned`` / ``case_escalated`` /
``renewal_at_risk`` / ``qbr_overdue`` vocabulary, the renewal-at-risk
dedup helper, and the NotificationKind constants.
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
    cust = Customer(
        tenant_id=tenant.id,
        name="Northstar",
        strongest_connection_user_id=csm.id,
    )
    sync_session.add(cust)
    sync_session.commit()
    sync_session.refresh(tenant)
    sync_session.refresh(csm)
    sync_session.refresh(cust)
    return tenant, csm, cust


# ── Vocabulary constants ───────────────────────────────────────────────


def test_notification_kind_constants_match_migration():
    """Confirm the four new constants are exposed and equal to the
    migration's string values. Catches typos before they ship."""
    from backend.app.services.notifications import NotificationKind, VALID_KINDS

    assert NotificationKind.CASE_ASSIGNED == "case_assigned"
    assert NotificationKind.CASE_ESCALATED == "case_escalated"
    assert NotificationKind.RENEWAL_AT_RISK == "renewal_at_risk"
    assert NotificationKind.QBR_OVERDUE == "qbr_overdue"
    for v in (
        "case_assigned",
        "case_escalated",
        "renewal_at_risk",
        "qbr_overdue",
    ):
        assert v in VALID_KINDS


# ── Renewal-at-risk dedup ─────────────────────────────────────────────


def test_renewal_at_risk_fires_when_score_high(sync_session, seeded):
    """A customer whose composite renewal-risk lands in the high band
    (>= 70) and has an owner should trigger the fire helper."""
    from backend.app.services.cs_account_health import should_fire_renewal_at_risk

    tenant, csm, cust = seeded
    cust.health_score = 10.0  # very low -> renewal_risk = 90
    sync_session.commit()
    assert should_fire_renewal_at_risk(sync_session, cust) is True


def test_renewal_at_risk_skips_when_score_low(sync_session, seeded):
    from backend.app.services.cs_account_health import should_fire_renewal_at_risk

    _t, _csm, cust = seeded
    cust.health_score = 90.0
    sync_session.commit()
    assert should_fire_renewal_at_risk(sync_session, cust) is False


def test_renewal_at_risk_skips_when_no_owner(sync_session, seeded):
    """Orphan accounts (no ``strongest_connection_user_id``) shouldn't
    fire — there's no one to notify, and notifying every CSM would
    flood low-priority cases."""
    from backend.app.services.cs_account_health import should_fire_renewal_at_risk

    _t, _csm, cust = seeded
    cust.health_score = 10.0
    cust.strongest_connection_user_id = None
    sync_session.commit()
    assert should_fire_renewal_at_risk(sync_session, cust) is False


def test_renewal_at_risk_dedupes_within_window(sync_session, seeded):
    """An unread renewal_at_risk for this customer in the dedup window
    suppresses a duplicate fire."""
    from backend.app.models import Notification
    from backend.app.services.cs_account_health import (
        RENEWAL_NOTIF_DEDUP_DAYS,
        should_fire_renewal_at_risk,
    )

    tenant, csm, cust = seeded
    cust.health_score = 10.0
    sync_session.commit()
    n = Notification(
        tenant_id=tenant.id,
        user_id=csm.id,
        kind="renewal_at_risk",
        title="Renewal risk: Northstar",
        link_url=f"/cs/accounts/{cust.id}",
    )
    sync_session.add(n)
    sync_session.flush()
    n.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    sync_session.commit()
    assert should_fire_renewal_at_risk(sync_session, cust) is False
    # Backdate beyond the dedup window — should fire again.
    n.created_at = datetime.now(timezone.utc) - timedelta(
        days=RENEWAL_NOTIF_DEDUP_DAYS + 1
    )
    sync_session.commit()
    assert should_fire_renewal_at_risk(sync_session, cust) is True


def test_renewal_at_risk_dedup_is_per_customer(sync_session, seeded):
    """A renewal_at_risk for customer A should NOT suppress one for
    customer B — link_url carries the id, dedup is per-row."""
    from backend.app.models import Customer, Notification
    from backend.app.services.cs_account_health import should_fire_renewal_at_risk

    tenant, csm, cust_a = seeded
    cust_a.health_score = 10.0
    cust_b = Customer(
        tenant_id=tenant.id,
        name="Polaris",
        strongest_connection_user_id=csm.id,
        health_score=10.0,
    )
    sync_session.add(cust_b)
    sync_session.commit()
    # A has a fresh notification; B should still fire.
    n = Notification(
        tenant_id=tenant.id,
        user_id=csm.id,
        kind="renewal_at_risk",
        title="Renewal risk: Northstar",
        link_url=f"/cs/accounts/{cust_a.id}",
    )
    sync_session.add(n)
    sync_session.commit()
    assert should_fire_renewal_at_risk(sync_session, cust_a) is False
    assert should_fire_renewal_at_risk(sync_session, cust_b) is True
