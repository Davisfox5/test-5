"""Tests for campaign variant winner-selection.

Uses an in-memory sync SQLite engine (same trick as
test_manager_anomaly_detector.py) seeded with sibling ``Campaign`` rows and
their ``CampaignEvent``s. Verifies that the winner is picked on the Wilson
lower bound of the engagement rate (not the raw rate), that low-sample
variants are excluded below ``MIN_SENDS_PER_VARIANT``, and that groups with
fewer than two eligible variants are skipped rather than decided.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from backend.app.services.campaign_winner_service import (
    MIN_SENDS_PER_VARIANT,
    decide_active_campaigns,
)


# SQLite needs JSONB/UUID compiled to JSON/CHAR — same trick db_fixtures uses.
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

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    sync_session.refresh(tenant)
    return tenant


def _make_campaign(session, tenant_id, *, name, variant, sent_count):
    from backend.app.models import Campaign

    now = datetime.now(timezone.utc)
    c = Campaign(
        tenant_id=tenant_id,
        name=name,
        channel="email",
        variant=variant,
        sent_count=sent_count,
        started_at=now - timedelta(days=7),
        ended_at=now - timedelta(days=1),
    )
    session.add(c)
    session.flush()
    return c


def _add_positive_events(session, tenant_id, campaign, count):
    from backend.app.models import CampaignEvent

    for _ in range(count):
        session.add(
            CampaignEvent(
                campaign_id=campaign.id,
                tenant_id=tenant_id,
                event_type="reply",
            )
        )
    session.flush()


def test_high_n_variant_beats_low_n_high_rate_variant(sync_session, seeded_tenant):
    """A 1/1 (or 30/30) variant must not beat a 990/1000 variant.

    Under the old raw-rate ranking, variant 'b' (30/30 = 100%) would win
    over variant 'a' (990/1000 = 99%). Under the Wilson-lower-bound
    ranking, 'a' — the much larger, still-excellent sample — wins because
    we can't be confident 'b's tiny sample really means 100%.
    """
    tenant_id = seeded_tenant.id
    campaign_a = _make_campaign(
        sync_session, tenant_id, name="fall-promo", variant="a", sent_count=1000
    )
    _add_positive_events(sync_session, tenant_id, campaign_a, 990)

    campaign_b = _make_campaign(
        sync_session,
        tenant_id,
        name="fall-promo",
        variant="b",
        sent_count=MIN_SENDS_PER_VARIANT,
    )
    _add_positive_events(sync_session, tenant_id, campaign_b, MIN_SENDS_PER_VARIANT)
    sync_session.commit()

    result = decide_active_campaigns(sync_session)
    assert result["decided"] == 1

    from backend.app.models import Experiment

    exp = (
        sync_session.query(Experiment)
        .filter(Experiment.type == "campaign_variant")
        .first()
    )
    assert exp is not None
    assert exp.result_summary["winner_variant"] == "a"
    # Both raw rate and Wilson lower bound must be recorded per variant.
    for row in exp.result_summary["all_variants"]:
        assert "rate" in row
        assert "rate_lower_bound" in row
    winner_row = next(
        r for r in exp.result_summary["all_variants"] if r["variant"] == "a"
    )
    loser_row = next(
        r for r in exp.result_summary["all_variants"] if r["variant"] == "b"
    )
    # Raw rate would have favored 'b' (100% > 99%) — the lower bound flips it.
    assert loser_row["rate"] > winner_row["rate"]
    assert winner_row["rate_lower_bound"] > loser_row["rate_lower_bound"]
    assert "confident" in exp.conclusion.lower()


def test_group_skipped_when_fewer_than_two_variants_meet_sample_floor(
    sync_session, seeded_tenant
):
    """A group where only one variant clears MIN_SENDS_PER_VARIANT is
    skipped entirely (re-evaluated on a later run) rather than decided."""
    tenant_id = seeded_tenant.id
    campaign_a = _make_campaign(
        sync_session, tenant_id, name="spring-promo", variant="a", sent_count=1000
    )
    _add_positive_events(sync_session, tenant_id, campaign_a, 500)

    campaign_b = _make_campaign(
        sync_session, tenant_id, name="spring-promo", variant="b", sent_count=1
    )
    _add_positive_events(sync_session, tenant_id, campaign_b, 1)
    sync_session.commit()

    result = decide_active_campaigns(sync_session)
    assert result["decided"] == 0
    assert result["skipped"] == 1

    from backend.app.models import Experiment

    exp = (
        sync_session.query(Experiment)
        .filter(Experiment.type == "campaign_variant")
        .first()
    )
    assert exp is None
