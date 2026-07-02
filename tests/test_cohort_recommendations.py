"""Tests for the cohort-driven predictive recommendation detectors.

Each detector returns ``RecommendationCandidate`` objects; the
``persist_candidates`` helper writes them as ``ManagerRecommendation``
rows with a (category, customer) dedup window.

Covers the trigger matrix for each detector + the dedup behaviour.
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
    from backend.app.models import Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    sync_session.refresh(tenant)
    return tenant


def _make_customer(
    sync_session,
    tenant,
    *,
    name="Acct",
    renewal_in_days=None,
    onboarding="completed",
):
    from backend.app.models import Customer

    cust = Customer(
        tenant_id=tenant.id,
        name=name,
        onboarding_status=onboarding,
        renewal_date=(
            date.today() + timedelta(days=renewal_in_days)
            if renewal_in_days is not None
            else None
        ),
    )
    sync_session.add(cust)
    sync_session.commit()
    sync_session.refresh(cust)
    return cust


def _add_interaction(
    sync_session, tenant, cust, *, domain, when, sentiment=None
):
    from backend.app.models import Interaction

    insights = {"sentiment_overall": sentiment} if sentiment else {}
    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=cust.id,
        channel="voice",
        domain=domain,
        insights=insights,
    )
    sync_session.add(ix)
    sync_session.flush()
    ix.created_at = when
    sync_session.commit()
    return ix


# ── detect_no_touch_renewal_risk ───────────────────────────────────────


def test_no_touch_renewal_fires_when_renewal_close_and_silent(
    sync_session, seeded
):
    from backend.app.services.cohort_recommendations import (
        detect_no_touch_renewal_risk,
    )

    cust = _make_customer(sync_session, seeded, renewal_in_days=20)
    out = detect_no_touch_renewal_risk(sync_session, seeded)
    assert any(c.customer_id == cust.id for c in out)
    rec = next(c for c in out if c.customer_id == cust.id)
    assert rec.category == "prevent_no_touch_churn"
    assert rec.score >= 60.0
    assert rec.evidence["days_to_renewal"] == 20


def test_no_touch_renewal_suppressed_by_recent_cs_touch(
    sync_session, seeded
):
    from backend.app.services.cohort_recommendations import (
        detect_no_touch_renewal_risk,
    )

    cust = _make_customer(sync_session, seeded, renewal_in_days=20)
    _add_interaction(
        sync_session,
        seeded,
        cust,
        domain="customer_service",
        when=datetime.now(timezone.utc) - timedelta(days=5),
    )
    out = detect_no_touch_renewal_risk(sync_session, seeded)
    assert not any(c.customer_id == cust.id for c in out)


def test_no_touch_renewal_ignores_far_renewals(sync_session, seeded):
    from backend.app.services.cohort_recommendations import (
        detect_no_touch_renewal_risk,
    )

    cust = _make_customer(sync_session, seeded, renewal_in_days=180)
    out = detect_no_touch_renewal_risk(sync_session, seeded)
    assert not any(c.customer_id == cust.id for c in out)


# ── detect_lead_stall ─────────────────────────────────────────────────


def test_lead_stall_fires_for_warm_silent_prospect(sync_session, seeded):
    from backend.app.services.cohort_recommendations import detect_lead_stall

    cust = _make_customer(sync_session, seeded, onboarding="not_started")
    _add_interaction(
        sync_session,
        seeded,
        cust,
        domain="sales",
        when=datetime.now(timezone.utc) - timedelta(days=35),
        sentiment="positive",
    )
    out = detect_lead_stall(sync_session, seeded)
    assert any(c.customer_id == cust.id for c in out)


def test_lead_stall_suppressed_by_recent_sales_touch(sync_session, seeded):
    from backend.app.services.cohort_recommendations import detect_lead_stall

    cust = _make_customer(sync_session, seeded, onboarding="not_started")
    _add_interaction(
        sync_session,
        seeded,
        cust,
        domain="sales",
        when=datetime.now(timezone.utc) - timedelta(days=5),
        sentiment="positive",
    )
    out = detect_lead_stall(sync_session, seeded)
    assert not any(c.customer_id == cust.id for c in out)


def test_lead_stall_skips_known_negative_lead(sync_session, seeded):
    """Negative sentiment = explicit no-go; we don't recommend
    re-engagement."""
    from backend.app.services.cohort_recommendations import detect_lead_stall

    cust = _make_customer(sync_session, seeded, onboarding="not_started")
    _add_interaction(
        sync_session,
        seeded,
        cust,
        domain="sales",
        when=datetime.now(timezone.utc) - timedelta(days=35),
        sentiment="negative",
    )
    out = detect_lead_stall(sync_session, seeded)
    assert not any(c.customer_id == cust.id for c in out)


def test_lead_stall_skips_cold_lead(sync_session, seeded):
    """No touch in 90+ days = cold; this detector is for warm-but-
    stalled, the lead-revival cohort. Different play."""
    from backend.app.services.cohort_recommendations import detect_lead_stall

    cust = _make_customer(sync_session, seeded, onboarding="not_started")
    _add_interaction(
        sync_session,
        seeded,
        cust,
        domain="sales",
        when=datetime.now(timezone.utc) - timedelta(days=120),
        sentiment="positive",
    )
    out = detect_lead_stall(sync_session, seeded)
    assert not any(c.customer_id == cust.id for c in out)


# ── detect_repeat_support_churn_risk ──────────────────────────────────


def test_repeat_support_fires_at_threshold(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.cohort_recommendations import (
        REPEAT_SUPPORT_THRESHOLD,
        detect_repeat_support_churn_risk,
    )

    cust = _make_customer(sync_session, seeded)
    now = datetime.now(timezone.utc)
    for i in range(REPEAT_SUPPORT_THRESHOLD):
        case = SupportCase(
            tenant_id=seeded.id,
            customer_id=cust.id,
            subject=f"i{i}",
            status="resolved",
        )
        sync_session.add(case)
        sync_session.flush()
        case.opened_at = now - timedelta(days=10 + i)
    sync_session.commit()
    out = detect_repeat_support_churn_risk(sync_session, seeded)
    assert any(c.customer_id == cust.id for c in out)


def test_repeat_support_suppressed_below_threshold(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.cohort_recommendations import (
        detect_repeat_support_churn_risk,
    )

    cust = _make_customer(sync_session, seeded)
    now = datetime.now(timezone.utc)
    case = SupportCase(
        tenant_id=seeded.id,
        customer_id=cust.id,
        subject="only one",
        status="resolved",
    )
    sync_session.add(case)
    sync_session.flush()
    case.opened_at = now - timedelta(days=5)
    sync_session.commit()
    out = detect_repeat_support_churn_risk(sync_session, seeded)
    assert not any(c.customer_id == cust.id for c in out)


# ── Persist + dedup ───────────────────────────────────────────────────


def test_persist_inserts_and_dedupes_within_window(sync_session, seeded):
    from backend.app.models import ManagerRecommendation
    from backend.app.services.cohort_recommendations import (
        RecommendationCandidate,
        persist_candidates,
    )

    cust = _make_customer(sync_session, seeded)
    cand = RecommendationCandidate(
        category="prevent_no_touch_churn",
        domain="customer_service",
        title="Schedule a CS touch with Acct before renewal",
        rationale="r",
        customer_id=cust.id,
        score=70.0,
        evidence={"customer_count": 1},
        target={"customer_id": str(cust.id)},
    )
    first = persist_candidates(sync_session, seeded.id, [cand])
    assert len(first) == 1
    # Re-run same candidate — should dedup.
    second = persist_candidates(sync_session, seeded.id, [cand])
    assert len(second) == 0
    rows = (
        sync_session.execute(
            select(ManagerRecommendation).where(
                ManagerRecommendation.tenant_id == seeded.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1


def test_run_for_tenant_returns_per_detector_counts(sync_session, seeded, monkeypatch):
    from backend.app.services.cohort_recommendations import run_for_tenant

    queued = []
    monkeypatch.setattr(
        "backend.app.services.recommendation_enrichment.queue_enrichment_for",
        lambda rows: queued.append(rows),
    )

    out = run_for_tenant(sync_session, seeded)
    # No customers => no candidates from any detector.
    assert out == {
        "no_touch_renewal_risk": 0,
        "lead_stall": 0,
        "repeat_support_churn_risk": 0,
        "inserted": 0,
    }
    # No rows inserted => the enrichment queue is never touched.
    assert queued == []


def test_run_for_tenant_queues_enrichment_for_inserted_rows(
    sync_session, seeded, monkeypatch
):
    from backend.app.services.cohort_recommendations import run_for_tenant

    cust = _make_customer(sync_session, seeded, renewal_in_days=20)

    queued = []
    monkeypatch.setattr(
        "backend.app.services.recommendation_enrichment.queue_enrichment_for",
        lambda rows: queued.append(rows),
    )

    out = run_for_tenant(sync_session, seeded)
    assert out["inserted"] == 1
    assert len(queued) == 1
    queued_rows = queued[0]
    assert len(queued_rows) == 1
    assert queued_rows[0].target.get("customer_id") == str(cust.id)
