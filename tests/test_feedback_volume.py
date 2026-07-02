"""Tests for ``feedback_service.feedback_volume_by_day`` — the read path
that unions live ``feedback_events`` counts with the ``feedback_daily_rollup``
history the retention sweep writes (see ``event_retention.py``), so a
volume-over-time chart doesn't fall off a cliff at the raw-retention
horizon.

Uses the shared in-memory async SQLite fixtures from ``db_fixtures.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.app.services import feedback_service

pytestmark = pytest.mark.asyncio


async def test_volume_unions_live_and_rollup_counts(test_session, test_tenant):
    from backend.app.models import FeedbackDailyRollup, FeedbackEvent

    today = datetime.now(timezone.utc)
    recent_day = today - timedelta(days=2)  # still in the live table
    old_day = date.today() - timedelta(days=500)  # long past raw retention

    # Live rows for a recent day.
    test_session.add_all(
        [
            FeedbackEvent(
                tenant_id=test_tenant.id,
                surface="email_reply",
                event_type="reply_sent_unchanged",
                signal_type="implicit",
                created_at=recent_day,
            ),
            FeedbackEvent(
                tenant_id=test_tenant.id,
                surface="email_reply",
                event_type="reply_sent_unchanged",
                signal_type="implicit",
                created_at=recent_day,
            ),
        ]
    )
    # Rollup row for a day so old the raw rows have already been swept.
    test_session.add(
        FeedbackDailyRollup(
            tenant_id=test_tenant.id,
            day=old_day,
            surface="email_reply",
            event_type="reply_sent_unchanged",
            count=41,
        )
    )
    await test_session.commit()

    volume = await feedback_service.feedback_volume_by_day(
        test_session, test_tenant.id, days=600
    )

    by_day = {row["day"]: row["count"] for row in volume}
    assert by_day[recent_day.date().isoformat()] == 2
    assert by_day[old_day.isoformat()] == 41


async def test_volume_prefers_rollup_when_both_exist_for_same_day(
    test_session, test_tenant
):
    from backend.app.models import FeedbackDailyRollup, FeedbackEvent

    same_day = datetime.now(timezone.utc) - timedelta(days=1)

    test_session.add(
        FeedbackEvent(
            tenant_id=test_tenant.id,
            surface="email_classifier",
            event_type="classification_overridden",
            signal_type="explicit",
            created_at=same_day,
        )
    )
    test_session.add(
        FeedbackDailyRollup(
            tenant_id=test_tenant.id,
            day=same_day.date(),
            surface="email_classifier",
            event_type="classification_overridden",
            count=99,
        )
    )
    await test_session.commit()

    volume = await feedback_service.feedback_volume_by_day(
        test_session, test_tenant.id, days=30
    )

    matches = [
        row
        for row in volume
        if row["day"] == same_day.date().isoformat()
        and row["surface"] == "email_classifier"
    ]
    assert len(matches) == 1
    assert matches[0]["count"] == 99


async def test_volume_scoped_to_tenant(test_session, test_tenant):
    from backend.app.models import FeedbackEvent, Tenant

    other_tenant = Tenant(name="Other Co", slug="other-co-vol")
    test_session.add(other_tenant)
    await test_session.commit()
    await test_session.refresh(other_tenant)

    test_session.add(
        FeedbackEvent(
            tenant_id=other_tenant.id,
            surface="analysis",
            event_type="insight_upvoted",
            signal_type="explicit",
            created_at=datetime.now(timezone.utc),
        )
    )
    await test_session.commit()

    volume = await feedback_service.feedback_volume_by_day(
        test_session, test_tenant.id, days=30
    )
    assert volume == []
