"""Tests for the manager-side anomaly detector.

Uses an in-memory sync SQLite engine seeded with a tenant, a baseline
of low-volume interactions, and a recent spike. Verifies that the
topic-spike detector fires once, that the fingerprint dedupes on a
second run, and that auto-resolve clears the alert once the spike
subsides.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


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


def _add_interaction(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    when: datetime,
    topics=None,
    sentiment=None,
    churn=None,
):
    from backend.app.models import Interaction

    insights = {}
    if topics is not None:
        insights["topics"] = topics
    if sentiment is not None:
        insights["sentiment_score"] = sentiment
    if churn is not None:
        insights["churn_risk_signal"] = churn
    ix = Interaction(
        tenant_id=tenant_id,
        channel="voice",
        insights=insights,
    )
    session.add(ix)
    session.flush()
    ix.created_at = when  # override server_default
    session.flush()
    return ix


def test_topic_spike_fires_once_and_dedupes(sync_session, seeded_tenant):
    """Seed a 'refund_request' spike in the last 48h with empty baseline.

    Expect exactly one ``topic_spike`` alert, and a second scan to
    produce zero new alerts thanks to the active-fingerprint dedupe.
    """
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    for i in range(12):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            topics=[{"name": "refund_request", "mentions": 1}],
        )
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    topic_alerts = [a for a in inserted if a.kind == "topic_spike"]
    assert len(topic_alerts) == 1, f"expected 1 spike, got {len(topic_alerts)}: {[a.title for a in inserted]}"
    alert = topic_alerts[0]
    assert alert.severity in {"high", "medium"}
    assert (alert.evidence or {}).get("topic") == "refund_request"

    # Re-run: partial-unique index keeps the fingerprint slot active,
    # so the detector returns no new rows.
    again = scan_tenant(sync_session, seeded_tenant)
    again_topic = [a for a in again if a.kind == "topic_spike"]
    assert again_topic == []


def test_resolve_stale_clears_alert_when_volume_drops(sync_session, seeded_tenant):
    """Insert an alert, drop the underlying volume, run resolve_stale,
    expect ``resolved_at`` to populate."""
    from backend.app.services.anomaly_detector import (
        resolve_stale,
        scan_tenant,
    )
    from backend.app.models import ManagerAlert

    now = datetime.now(timezone.utc)
    for i in range(12):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            topics=[{"name": "exporter_bug", "mentions": 1}],
        )
    sync_session.commit()

    scan_tenant(sync_session, seeded_tenant)
    alert = sync_session.query(ManagerAlert).first()
    assert alert is not None
    # Backdate so the auto-resolver considers it.
    alert.opened_at = now - timedelta(hours=24)
    sync_session.commit()

    # Wipe all interactions to make the condition no longer hold.
    from backend.app.models import Interaction

    sync_session.query(Interaction).delete()
    sync_session.commit()

    resolved = resolve_stale(sync_session)
    assert resolved >= 1
    sync_session.refresh(alert)
    assert alert.resolved_at is not None


def test_sentiment_drop_detector_below_threshold_noop(sync_session, seeded_tenant):
    """No alert when recent sentiment matches the baseline."""
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    # 15 recent calls and 15 baseline calls, both at 7.5 — no drop.
    for i in range(15):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            sentiment=7.5,
        )
    for i in range(15):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(days=2 + i / 2),
            sentiment=7.5,
        )
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    drops = [a for a in inserted if a.kind == "sentiment_drop"]
    assert drops == []


def test_sentiment_drop_detector_fires_on_real_drop(sync_session, seeded_tenant):
    """Baseline at 8.0, recent at 5.0 — should fire at 'high' severity."""
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    for i in range(15):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            sentiment=5.0,
        )
    for i in range(30):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(days=2 + i / 4),
            sentiment=8.0,
        )
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    drops = [a for a in inserted if a.kind == "sentiment_drop"]
    assert len(drops) == 1
    assert drops[0].severity in {"high", "medium"}


def test_alert_title_has_no_em_dashes(sync_session, seeded_tenant):
    """Plain-English voice rule applies to detector-generated titles."""
    from backend.app.services.anomaly_detector import scan_tenant

    now = datetime.now(timezone.utc)
    for i in range(12):
        _add_interaction(
            sync_session,
            seeded_tenant.id,
            when=now - timedelta(hours=i + 1),
            topics=[{"name": "billing-issue", "mentions": 1}],
        )
    sync_session.commit()

    inserted = scan_tenant(sync_session, seeded_tenant)
    assert inserted
    for alert in inserted:
        assert "—" not in alert.title
        assert "–" not in alert.title
