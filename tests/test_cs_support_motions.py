"""Tests for the CS + IT-Support motion plumbing landed in ``dom_002``.

Covers the new alert detectors, the per-motion recommendation
category whitelist, the analysis-prompt registry, the manager-voice
rules dispatcher, and the SupportCase model's basic lifecycle.

Uses the same in-memory sync SQLite fixture pattern as
``test_manager_anomaly_detector.py``.
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
    import backend.app.models  # noqa: F401 — registers mapped classes

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded_tenant(sync_session):
    from backend.app.models import Tenant

    tenant = Tenant(name="Acme CS", slug=f"acme-cs-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    sync_session.refresh(tenant)
    return tenant


def _add_interaction(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    when: datetime,
    domain: str,
    churn: str = None,
    sentiment: float = None,
):
    from backend.app.models import Interaction

    insights = {}
    if churn is not None:
        insights["churn_risk_signal"] = churn
    if sentiment is not None:
        insights["sentiment_score"] = sentiment
    ix = Interaction(
        tenant_id=tenant_id,
        channel="voice",
        domain=domain,
        insights=insights,
    )
    session.add(ix)
    session.flush()
    ix.created_at = when
    session.flush()
    return ix


# ── Analysis-prompt registry ───────────────────────────────────────────


def test_analysis_prompt_registry_covers_all_domains():
    from backend.app.services.ai_analysis import (
        ANALYSIS_SYSTEM_PROMPT_BY_DOMAIN,
        ANALYSIS_SYSTEM_PROMPT,
        ANALYSIS_SYSTEM_PROMPT_CS,
        ANALYSIS_SYSTEM_PROMPT_IT_SUPPORT,
        _system_prompt_for_domain,
    )

    assert set(ANALYSIS_SYSTEM_PROMPT_BY_DOMAIN.keys()) == {
        "sales",
        "customer_service",
        "it_support",
        "generic",
    }
    assert _system_prompt_for_domain("sales") is ANALYSIS_SYSTEM_PROMPT
    assert _system_prompt_for_domain("customer_service") is ANALYSIS_SYSTEM_PROMPT_CS
    assert _system_prompt_for_domain("it_support") is ANALYSIS_SYSTEM_PROMPT_IT_SUPPORT
    # Unknown / NULL falls back to sales (the production-validated rubric).
    assert _system_prompt_for_domain(None) is ANALYSIS_SYSTEM_PROMPT
    assert _system_prompt_for_domain("not_a_domain") is ANALYSIS_SYSTEM_PROMPT


# ── Manager-voice rules dispatcher ─────────────────────────────────────


def test_manager_voice_rules_per_domain_frames_correct_audience():
    from backend.app.services.plain_english import (
        MANAGER_VOICE_RULES,
        MANAGER_VOICE_RULES_SALES,
        MANAGER_VOICE_RULES_CUSTOMER_SERVICE,
        MANAGER_VOICE_RULES_IT_SUPPORT,
        manager_voice_rules_for,
    )

    assert MANAGER_VOICE_RULES is MANAGER_VOICE_RULES_SALES
    sales = manager_voice_rules_for("sales")
    cs = manager_voice_rules_for("customer_service")
    it = manager_voice_rules_for("it_support")
    assert sales is MANAGER_VOICE_RULES_SALES
    assert cs is MANAGER_VOICE_RULES_CUSTOMER_SERVICE
    assert it is MANAGER_VOICE_RULES_IT_SUPPORT
    assert "sales floor" in sales
    assert "customer-success" in cs.lower()
    assert "support" in it.lower()
    assert manager_voice_rules_for(None) is MANAGER_VOICE_RULES_SALES


# ── Detectors ──────────────────────────────────────────────────────────


def test_renewal_risk_spike_fires_on_cs_high_churn(sync_session, seeded_tenant):
    """5 CS interactions tagged ``churn_risk_signal=high`` in 24h with no
    historical baseline should fire one ``renewal_risk_spike`` alert."""
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    for i in range(6):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            domain="customer_service",
            churn="high",
        )
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    renewals = [a for a in inserted if a.kind == "renewal_risk_spike"]
    assert len(renewals) == 1
    alert = renewals[0]
    assert alert.domain == "customer_service"
    assert "—" not in alert.title  # plain-English voice scrub


def test_renewal_risk_spike_ignores_sales_interactions(
    sync_session, seeded_tenant
):
    """High-churn signals on Sales interactions should NOT trigger the
    CS renewal-risk detector. This is the load-bearing motion-scope
    test: a noisy sales week shouldn't page the CS manager."""
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    for i in range(10):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            domain="sales",
            churn="high",
        )
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    renewals = [a for a in inserted if a.kind == "renewal_risk_spike"]
    assert renewals == []


def test_escalation_surge_fires_on_support_case_escalations(
    sync_session, seeded_tenant
):
    """5 cases escalated in the last 24h with no baseline should fire."""
    from backend.app.models import SupportCase
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    for i in range(6):
        case = SupportCase(
            tenant_id=seeded_tenant.id,
            subject=f"issue {i}",
            status="escalated",
        )
        sync_session.add(case)
        sync_session.flush()
        case.opened_at = now - timedelta(hours=i + 2)
        case.escalated_at = now - timedelta(hours=i + 1)
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    surges = [a for a in inserted if a.kind == "escalation_surge"]
    assert len(surges) == 1
    assert surges[0].domain == "it_support"


def test_csat_drop_support_fires_on_low_csat(sync_session, seeded_tenant):
    """Baseline CSAT 4.5, recent 2.5 — should fire."""
    from backend.app.models import SupportCase
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    # Baseline: 30 resolved cases over 14d at CSAT 4 or 5.
    for i in range(30):
        case = SupportCase(
            tenant_id=seeded_tenant.id,
            subject=f"old {i}",
            status="resolved",
            csat_score=5 if i % 2 == 0 else 4,
        )
        sync_session.add(case)
        sync_session.flush()
        case.opened_at = now - timedelta(days=2 + i / 4, hours=1)
        case.resolved_at = now - timedelta(days=2 + i / 4)

    # Recent 24h: 6 resolved cases at CSAT 1-2.
    for i in range(6):
        case = SupportCase(
            tenant_id=seeded_tenant.id,
            subject=f"recent {i}",
            status="resolved",
            csat_score=1 if i % 2 == 0 else 2,
        )
        sync_session.add(case)
        sync_session.flush()
        case.opened_at = now - timedelta(hours=i + 2)
        case.resolved_at = now - timedelta(hours=i + 1)
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    drops = [a for a in inserted if a.kind == "csat_drop_support"]
    assert len(drops) == 1
    assert drops[0].domain == "it_support"
    assert drops[0].severity in {"high", "medium"}


# ── Recommendation category whitelist ──────────────────────────────────


def test_recommendation_categories_are_motion_specific():
    from backend.app.services.manager_recommendation_builder import (
        _VALID_CATEGORIES_BY_DOMAIN,
        _system_prompt_for,
    )

    sales = _VALID_CATEGORIES_BY_DOMAIN["sales"]
    cs = _VALID_CATEGORIES_BY_DOMAIN["customer_service"]
    it = _VALID_CATEGORIES_BY_DOMAIN["it_support"]
    # The three motions should not share any category.
    assert not (sales & cs)
    assert not (cs & it)
    assert not (sales & it)
    # System-prompt for each domain mentions its own categories.
    cs_prompt = _system_prompt_for("customer_service")
    for cat in cs:
        assert cat in cs_prompt
    it_prompt = _system_prompt_for("it_support")
    for cat in it:
        assert cat in it_prompt


# ── SupportCase basics ─────────────────────────────────────────────────


def test_support_case_creates_with_default_lifecycle(sync_session, seeded_tenant):
    from backend.app.models import SupportCase

    case = SupportCase(
        tenant_id=seeded_tenant.id,
        subject="VPN drops every morning",
    )
    sync_session.add(case)
    sync_session.commit()
    sync_session.refresh(case)
    assert case.status == "open"
    assert case.priority == "medium"
    assert case.first_contact_resolution is None
    assert case.csat_score is None
    assert case.opened_at is not None
    assert case.resolved_at is None


def test_support_case_lifecycle_transitions(sync_session, seeded_tenant):
    from backend.app.models import SupportCase

    case = SupportCase(
        tenant_id=seeded_tenant.id,
        subject="printer not responding",
        priority="high",
    )
    sync_session.add(case)
    sync_session.commit()
    case.status = "in_progress"
    case.first_response_at = datetime.now(timezone.utc)
    sync_session.commit()
    case.status = "resolved"
    case.resolved_at = datetime.now(timezone.utc)
    case.first_contact_resolution = True
    case.csat_score = 5
    sync_session.commit()
    sync_session.refresh(case)
    assert case.status == "resolved"
    assert case.first_contact_resolution is True
    assert case.csat_score == 5


# ── Auth helpers for domain scopes ─────────────────────────────────────


def test_require_domain_manager_rejects_unknown_domain():
    from backend.app.auth import require_domain_manager

    with pytest.raises(ValueError, match="unknown domain"):
        require_domain_manager("not_a_domain")


def test_require_domain_agent_rejects_unknown_domain():
    from backend.app.auth import require_domain_agent

    with pytest.raises(ValueError, match="unknown domain"):
        require_domain_agent("not_a_domain")


def test_auth_principal_helpers_handle_admin_scope():
    from types import SimpleNamespace
    from backend.app.auth import AuthPrincipal, CANONICAL_DOMAINS

    p = AuthPrincipal(
        tenant=SimpleNamespace(id=uuid.uuid4()),
        user=SimpleNamespace(id=uuid.uuid4()),
        role="admin",
        source="session",
        agent_domains=["customer_service"],
        manager_domains=["customer_service"],
        is_tenant_admin=True,
    )
    # Tenant admins can manage every domain even if not in manager_domains.
    for d in CANONICAL_DOMAINS:
        assert p.can_manage_domain(d) is True
    # Agent-scope is still literal even for tenant admins.
    assert p.can_act_in_domain("customer_service") is True
    assert p.can_act_in_domain("sales") is False


def test_auth_principal_helpers_non_admin_uses_lists():
    from types import SimpleNamespace
    from backend.app.auth import AuthPrincipal

    p = AuthPrincipal(
        tenant=SimpleNamespace(id=uuid.uuid4()),
        user=SimpleNamespace(id=uuid.uuid4()),
        role="agent",
        source="session",
        agent_domains=["sales"],
        manager_domains=[],
        is_tenant_admin=False,
    )
    assert p.can_act_in_domain("sales") is True
    assert p.can_act_in_domain("customer_service") is False
    assert p.can_manage_domain("sales") is False
