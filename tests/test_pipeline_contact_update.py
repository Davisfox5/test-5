"""Unit tests for ``update_contact_rollup`` — the Step 13b helper that
wires per-interaction AI insights into contact-level trend fields.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.app.tasks import (
    CONTACT_SENTIMENT_TREND_CAP,
    update_contact_rollup,
)


def _contact(trend=None, count=0, last_seen=None):
    return SimpleNamespace(
        id="c-1",
        sentiment_trend=trend or [],
        interaction_count=count,
        last_seen_at=last_seen,
    )


def test_appends_sentiment_score_and_bumps_count():
    contact = _contact(trend=[6.0], count=2)
    when = datetime(2026, 4, 17, 10, 0)
    update_contact_rollup(contact, {"sentiment_score": 7.5}, when)

    assert contact.sentiment_trend == [6.0, 7.5]
    assert contact.interaction_count == 3
    assert contact.last_seen_at == when


def test_handles_missing_sentiment_score():
    contact = _contact(trend=[5.0], count=1)
    when = datetime(2026, 4, 17, 10, 0)
    update_contact_rollup(contact, {}, when)

    assert contact.sentiment_trend == [5.0]  # unchanged
    assert contact.interaction_count == 2
    assert contact.last_seen_at == when


def test_caps_trend_at_50_entries():
    contact = _contact(trend=list(range(60)), count=60)
    when = datetime(2026, 4, 17, 10, 0)
    update_contact_rollup(contact, {"sentiment_score": 9.0}, when)

    assert len(contact.sentiment_trend) == CONTACT_SENTIMENT_TREND_CAP
    # FIFO: oldest entries dropped, newest kept at tail.
    assert contact.sentiment_trend[-1] == 9.0
    assert contact.sentiment_trend[0] > 0  # oldest zero-entries were dropped


def test_tolerates_non_numeric_sentiment_score():
    contact = _contact(trend=[3.0], count=1)
    when = datetime(2026, 4, 17, 10, 0)
    update_contact_rollup(contact, {"sentiment_score": "not-a-number"}, when)

    assert contact.sentiment_trend == [3.0]  # unchanged — bad value skipped
    assert contact.interaction_count == 2  # but count still bumps
    assert contact.last_seen_at == when


def test_handles_null_existing_trend():
    contact = _contact(trend=None, count=0)
    when = datetime(2026, 4, 17, 10, 0)
    update_contact_rollup(contact, {"sentiment_score": 8.0}, when)

    assert contact.sentiment_trend == [8.0]
    assert contact.interaction_count == 1


def test_last_seen_advances_on_each_call():
    contact = _contact()
    t1 = datetime(2026, 4, 17, 10, 0)
    t2 = t1 + timedelta(hours=3)

    update_contact_rollup(contact, {"sentiment_score": 6.0}, t1)
    update_contact_rollup(contact, {"sentiment_score": 7.0}, t2)

    assert contact.last_seen_at == t2
    assert contact.sentiment_trend == [6.0, 7.0]
    assert contact.interaction_count == 2
