"""Tests for ``backend.app.services.tenant_insights_service``.

These tests mock ``Session.execute`` and return canned row lists for each
SQL query so we can assert the aggregation function builds the expected
JSON document shape without needing a real Postgres instance.
"""

from datetime import date
from types import SimpleNamespace

import pytest


def _row(*values):
    """Build a tuple-like row that supports both index and iteration."""
    return tuple(values)


def _result(rows):
    """Mimic SQLAlchemy Result.fetchone / fetchall on a list of tuples."""
    return SimpleNamespace(
        fetchone=lambda: rows[0] if rows else None,
        fetchall=lambda: list(rows),
    )


class _ScriptedSession:
    """Hands back canned results in the order aggregate_tenant_period queries."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self._calls = 0

    def execute(self, stmt, params=None):
        assert self._calls < len(self._scripted), (
            f"Unexpected {self._calls + 1}th query — only "
            f"{len(self._scripted)} scripted."
        )
        rows = self._scripted[self._calls]
        self._calls += 1
        return _result(rows)


def test_aggregate_tenant_period_builds_expected_shape():
    from backend.app.services.tenant_insights_service import aggregate_tenant_period

    # Script results in the exact order aggregate_tenant_period issues them:
    # 1. sentiment summary  2. topics  3. competitors  4. product_feedback
    # 5. coaching adherence  6. compliance_gaps  7. improvements  8. strengths
    # 9. signal buckets  10. avg_risk  11. channel_mix
    scripted = [
        [_row(42, 7.5)],  # sentiment
        [_row("pricing", 30, 0.8), _row("onboarding", 12, 0.4)],  # topics
        [_row("BasicCall", 5, 3), _row("RivalCo", 2, 0)],  # competitors
        [_row("mobile-app", 2, 8, 1, "We need dark mode")],  # product_feedback
        [_row(88.0)],  # script_adherence avg
        [_row("missed disclosure", 4), _row("skipped recap", 2)],  # gaps
        [_row("ask for the close", 3)],  # improvements
        [_row("clear agenda", 6)],  # strengths
        [_row("high", "medium", 3), _row("none", "low", 20)],  # buckets
        [_row(0.42, 0.31)],  # avg risk
        [_row("voice", 30, 7.8), _row("email", 12, 6.9)],  # channel_mix
    ]

    session = _ScriptedSession(scripted)
    doc = aggregate_tenant_period(
        session, tenant_id="t-1",
        period_start=date(2026, 4, 10), period_end=date(2026, 4, 17),
    )

    # Sentiment
    assert doc["sentiment"] == {"total_interactions": 42, "avg_sentiment_score": 7.5}

    # Topics — two entries, sorted by mentions (query already ORDERs).
    assert doc["topics"][0] == {"name": "pricing", "mentions": 30, "avg_relevance": 0.8}
    assert doc["topics"][1]["name"] == "onboarding"

    # Competitors — handled_well_pct is rounded.
    assert doc["competitors"][0] == {
        "competitor": "BasicCall",
        "mentions": 5,
        "handled_well": 3,
        "handled_well_pct": 60.0,
    }
    assert doc["competitors"][1]["handled_well_pct"] == 0.0

    # Product feedback
    assert doc["product_feedback"][0] == {
        "theme": "mobile-app",
        "positive_count": 2,
        "negative_count": 8,
        "neutral_count": 1,
        "sample_quote": "We need dark mode",
    }

    # Coaching
    assert doc["coaching"]["avg_script_adherence"] == 88.0
    assert doc["coaching"]["top_compliance_gaps"][0] == {"text": "missed disclosure", "count": 4}
    assert doc["coaching"]["top_improvements"][0]["text"] == "ask for the close"
    assert doc["coaching"]["top_strengths"][0]["text"] == "clear agenda"

    # Signals — only (high, medium, 3) contributes to both buckets; (none, low, 20)
    # contributes to churn.none and upsell.low.
    assert doc["signals"]["churn"] == {"high": 3, "medium": 0, "low": 0, "none": 20}
    assert doc["signals"]["upsell"] == {"high": 0, "medium": 3, "low": 20, "none": 0}
    assert doc["signals"]["avg_churn_risk"] == 0.42
    assert doc["signals"]["avg_upsell_score"] == 0.31

    # Channel mix
    assert doc["channel_mix"][0] == {"channel": "voice", "count": 30, "avg_sentiment": 7.8}


def test_aggregate_handles_empty_tenant():
    from backend.app.services.tenant_insights_service import aggregate_tenant_period

    # All queries return empty rowsets.
    scripted = [
        [_row(0, None)],  # sentiment
        [],               # topics
        [],               # competitors
        [],               # product_feedback
        [_row(None)],     # adherence
        [], [], [],       # gaps, improvements, strengths
        [],               # buckets
        [_row(None, None)],  # avg risk
        [],               # channel_mix
    ]
    session = _ScriptedSession(scripted)
    doc = aggregate_tenant_period(
        session, tenant_id="t-1",
        period_start=date(2026, 4, 10), period_end=date(2026, 4, 17),
    )

    assert doc["sentiment"]["total_interactions"] == 0
    assert doc["sentiment"]["avg_sentiment_score"] is None
    assert doc["topics"] == []
    assert doc["competitors"] == []
    assert doc["product_feedback"] == []
    assert doc["coaching"]["avg_script_adherence"] is None
    assert doc["signals"]["churn"] == {"high": 0, "medium": 0, "low": 0, "none": 0}
    assert doc["channel_mix"] == []
