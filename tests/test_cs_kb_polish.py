"""Tests for the PR C surface: CS health computation, renewal risk
composite, KB-article-request model, per-domain alert threshold
overrides, Slack domain-channel routing.

Sticks to the project's in-memory SQLite fixture style.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

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

    tenant = Tenant(name="Acme CS+KB", slug=f"acme-cs-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    cust = Customer(tenant_id=tenant.id, name="Northstar Logistics")
    sync_session.add(cust)
    sync_session.commit()
    sync_session.refresh(tenant)
    sync_session.refresh(cust)
    return tenant, cust


def _add_cs_interaction(
    sync_session, tenant, customer, *, when=None, sentiment=None, churn=None
):
    from backend.app.models import Interaction

    insights = {}
    if sentiment is not None:
        insights["sentiment_score"] = sentiment
    if churn is not None:
        insights["churn_risk_signal"] = churn
    ix = Interaction(
        tenant_id=tenant.id,
        customer_id=customer.id,
        channel="voice",
        domain="customer_service",
        insights=insights,
    )
    sync_session.add(ix)
    sync_session.flush()
    if when is not None:
        ix.created_at = when
    sync_session.commit()
    return ix


# ── Health score ──────────────────────────────────────────────────────


def test_health_no_interactions_returns_neutral_middle(sync_session, seeded):
    from backend.app.services.cs_account_health import compute_health_score

    _tenant, cust = seeded
    b = compute_health_score(sync_session, cust)
    # 0 engagement, but sentiment / churn fallbacks land mid.
    assert b.cs_interaction_count == 0
    assert 30 <= b.overall <= 75


def test_health_high_engagement_high_sentiment_no_churn_is_high(
    sync_session, seeded
):
    from backend.app.services.cs_account_health import compute_health_score

    tenant, cust = seeded
    now = datetime.now(timezone.utc)
    for i in range(6):
        _add_cs_interaction(
            sync_session,
            tenant,
            cust,
            when=now - timedelta(days=i * 5),
            sentiment=8.5,
            churn="none",
        )
    b = compute_health_score(sync_session, cust)
    assert b.overall > 75


def test_health_high_churn_signals_lowers_score(sync_session, seeded):
    from backend.app.services.cs_account_health import compute_health_score

    tenant, cust = seeded
    now = datetime.now(timezone.utc)
    for i in range(5):
        _add_cs_interaction(
            sync_session,
            tenant,
            cust,
            when=now - timedelta(days=i * 3),
            sentiment=4.0,
            churn="high",
        )
    b = compute_health_score(sync_session, cust)
    # Combined: low sentiment (40) + zero churn-signal score on every
    # call lands the overall in the bottom half. Loose bound — the
    # exact mix depends on the engagement window cadence which we
    # don't pin here.
    assert b.overall < 50
    assert b.churn_signal < 20


def test_health_onboarding_status_buckets_apply(sync_session, seeded):
    from backend.app.services.cs_account_health import compute_health_score

    _tenant, cust = seeded
    cust.onboarding_status = "stalled"
    sync_session.commit()
    b_stalled = compute_health_score(sync_session, cust)
    cust.onboarding_status = "completed"
    sync_session.commit()
    b_done = compute_health_score(sync_session, cust)
    assert b_stalled.onboarding < b_done.onboarding
    assert b_stalled.overall < b_done.overall


def test_renewal_risk_score_higher_for_lower_health(sync_session, seeded):
    from backend.app.services.cs_account_health import (
        renewal_risk_score,
    )

    tenant, cust = seeded
    cust.health_score = 80.0
    sync_session.commit()
    healthy = renewal_risk_score(sync_session, cust)
    cust.health_score = 20.0
    sync_session.commit()
    unhealthy = renewal_risk_score(sync_session, cust)
    assert unhealthy > healthy
    # Compute path used when health_score is None.
    cust.health_score = None
    sync_session.commit()
    on_the_fly = renewal_risk_score(sync_session, cust)
    assert 0 <= on_the_fly <= 100


def test_renewal_risk_inflates_with_open_support_cases(sync_session, seeded):
    from backend.app.models import SupportCase
    from backend.app.services.cs_account_health import renewal_risk_score

    tenant, cust = seeded
    cust.health_score = 60.0
    sync_session.commit()
    baseline = renewal_risk_score(sync_session, cust)
    for i in range(3):
        case = SupportCase(
            tenant_id=tenant.id,
            customer_id=cust.id,
            subject=f"issue {i}",
            status="open",
        )
        sync_session.add(case)
    sync_session.commit()
    inflated = renewal_risk_score(sync_session, cust)
    assert inflated > baseline


def test_renewal_risk_clamps_inside_0_100(sync_session, seeded):
    from backend.app.services.cs_account_health import renewal_risk_score

    _tenant, cust = seeded
    cust.health_score = 100.0
    sync_session.commit()
    low = renewal_risk_score(sync_session, cust)
    cust.health_score = 0.0
    sync_session.commit()
    high = renewal_risk_score(sync_session, cust)
    assert 0 <= low <= 100
    assert 0 <= high <= 100


def test_list_upcoming_renewals_filters_by_date_window(sync_session, seeded):
    from backend.app.services.cs_account_health import list_upcoming_renewals

    tenant, cust = seeded
    today = date.today()
    cust.renewal_date = today + timedelta(days=45)
    sync_session.commit()
    rows_short = list_upcoming_renewals(sync_session, tenant.id, days_ahead=30)
    rows_long = list_upcoming_renewals(sync_session, tenant.id, days_ahead=90)
    assert rows_short == []
    assert len(rows_long) == 1
    assert rows_long[0]["customer_id"] == cust.id
    assert "renewal_risk_score" in rows_long[0]


# ── KBArticleRequest ──────────────────────────────────────────────────


def test_kb_article_request_default_lifecycle(sync_session, seeded):
    from backend.app.models import KBArticleRequest

    tenant, _cust = seeded
    r = KBArticleRequest(
        tenant_id=tenant.id,
        topic="VPN reauth steps for 2026 client",
    )
    sync_session.add(r)
    sync_session.commit()
    sync_session.refresh(r)
    assert r.status == "open"
    assert r.priority == "medium"
    assert r.published_at is None


def test_kb_article_request_round_trip_publish(sync_session, seeded):
    from backend.app.models import KBArticleRequest

    tenant, _cust = seeded
    r = KBArticleRequest(
        tenant_id=tenant.id,
        topic="t",
        proposed_body="new body",
        priority="high",
    )
    sync_session.add(r)
    sync_session.commit()
    r.status = "published"
    r.published_at = datetime.now(timezone.utc)
    sync_session.commit()
    sync_session.refresh(r)
    assert r.status == "published"
    assert r.priority == "high"
    assert r.published_at is not None


# ── Per-domain alert threshold override ────────────────────────────────


def test_threshold_helper_prefers_domain_override(sync_session, seeded):
    from backend.app.models import AlertChannelConfig, AlertDomainConfig
    from backend.app.services.anomaly_detector import _threshold

    tenant_cfg = AlertChannelConfig(
        tenant_id=seeded[0].id,
        sentiment_drop_threshold=1.5,
    )
    domain_cfg = AlertDomainConfig(
        tenant_id=seeded[0].id,
        domain="customer_service",
        sentiment_drop_threshold=2.5,
    )
    out = _threshold(
        domain_cfg, tenant_cfg, "sentiment_drop_threshold", 1.0
    )
    assert out == 2.5


def test_threshold_helper_falls_back_to_tenant_when_domain_null(
    sync_session, seeded
):
    from backend.app.models import AlertChannelConfig, AlertDomainConfig
    from backend.app.services.anomaly_detector import _threshold

    tenant_cfg = AlertChannelConfig(
        tenant_id=seeded[0].id,
        sentiment_drop_threshold=1.5,
    )
    domain_cfg = AlertDomainConfig(
        tenant_id=seeded[0].id,
        domain="customer_service",
        sentiment_drop_threshold=None,
    )
    out = _threshold(
        domain_cfg, tenant_cfg, "sentiment_drop_threshold", 1.0
    )
    assert out == 1.5


def test_threshold_helper_falls_back_to_default_when_both_null(
    sync_session, seeded
):
    from backend.app.models import AlertChannelConfig
    from backend.app.services.anomaly_detector import _threshold

    tenant_cfg = AlertChannelConfig(
        tenant_id=seeded[0].id,
        sentiment_drop_threshold=None,
    )
    out = _threshold(None, tenant_cfg, "sentiment_drop_threshold", 2.0)
    assert out == 2.0


# ── Slack channel routing ──────────────────────────────────────────────


def test_channel_for_alert_uses_domain_override():
    from backend.app.models import ManagerAlert, SlackIntegration
    from backend.app.services.manager_alert_fanout import _channel_for_alert

    slack = SlackIntegration(
        tenant_id=uuid.uuid4(),
        slack_team_id="t1",
        bot_token_encrypted="x",
        default_channel_id="C_default",
        domain_channel_map={
            "sales": "C_sales",
            "customer_service": "C_cs",
        },
    )
    alert_cs = ManagerAlert(
        tenant_id=slack.tenant_id,
        kind="health_score_drop",
        severity="medium",
        title="t",
        fingerprint="f",
        domain="customer_service",
    )
    assert _channel_for_alert(slack, alert_cs) == "C_cs"


def test_channel_for_alert_falls_back_to_default():
    from backend.app.models import ManagerAlert, SlackIntegration
    from backend.app.services.manager_alert_fanout import _channel_for_alert

    slack = SlackIntegration(
        tenant_id=uuid.uuid4(),
        slack_team_id="t1",
        bot_token_encrypted="x",
        default_channel_id="C_default",
        domain_channel_map={},
    )
    alert = ManagerAlert(
        tenant_id=slack.tenant_id,
        kind="topic_spike",
        severity="medium",
        title="t",
        fingerprint="f",
        domain="sales",
    )
    assert _channel_for_alert(slack, alert) == "C_default"


def test_channel_for_alert_handles_no_domain():
    from backend.app.models import ManagerAlert, SlackIntegration
    from backend.app.services.manager_alert_fanout import _channel_for_alert

    slack = SlackIntegration(
        tenant_id=uuid.uuid4(),
        slack_team_id="t1",
        bot_token_encrypted="x",
        default_channel_id="C_default",
        domain_channel_map={"sales": "C_sales"},
    )
    alert = ManagerAlert(
        tenant_id=slack.tenant_id,
        kind="topic_spike",
        severity="medium",
        title="t",
        fingerprint="f",
        domain=None,
    )
    assert _channel_for_alert(slack, alert) == "C_default"
