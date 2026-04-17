"""Smoke tests for Pydantic response models in analytics.py.

These don't hit the DB — they ensure the response contract (field names,
types, required vs optional) stays stable so the frontend code in
``website/js/demo.js`` keeps working.
"""

import uuid

import pytest


def test_business_health_accepts_full_payload():
    from backend.app.api.analytics import BusinessHealth

    payload = BusinessHealth(
        health_score=75.0,
        total_interactions=42,
        avg_sentiment=7.5,
        channels_breakdown=[
            {"channel": "voice", "count": 30, "avg_sentiment": 7.8},
            {"channel": "email", "count": 12, "avg_sentiment": 6.9},
        ],
        top_topics=[
            {"name": "pricing", "mentions": 30, "avg_relevance": 0.8},
            {"name": "onboarding", "mentions": 12},
        ],
    )
    assert payload.health_score == 75.0
    assert payload.top_topics[0].name == "pricing"
    assert payload.channels_breakdown[0].avg_sentiment == 7.8


def test_dashboard_summary_deltas_can_be_null():
    from backend.app.api.analytics import DashboardSummary

    payload = DashboardSummary(
        total_interactions=100,
        avg_sentiment_score=7.0,
        action_items_open=15,
        avg_qa_score=None,
        prev_period_deltas={
            "total_interactions_pct": 12.5,
            "avg_sentiment_pct": None,
            "avg_qa_pct": None,
        },
    )
    assert payload.avg_qa_score is None
    assert payload.prev_period_deltas["avg_sentiment_pct"] is None


def test_competitor_row_pct_is_float():
    from backend.app.api.analytics import CompetitorRow

    payload = CompetitorRow(
        competitor="BasicCall",
        mentions=5,
        handled_well=3,
        handled_well_pct=60.0,
    )
    assert payload.handled_well_pct == 60.0


def test_client_trends_exposes_both_numeric_and_categorical_churn():
    from backend.app.api.analytics import ClientTrends

    payload = ClientTrends(
        contact_id=uuid.uuid4(),
        sentiment_over_time=[{"date": "2026-04-17", "avg_sentiment": 7.5}],
        interaction_history=[],
        churn_risk=0.42,
        churn_risk_signal="medium",
    )
    assert payload.churn_risk == 0.42
    assert payload.churn_risk_signal == "medium"


def test_signal_buckets_defaults_all_keys_present():
    from backend.app.api.analytics import SignalBuckets

    payload = SignalBuckets(
        churn={"high": 1, "medium": 2, "low": 3, "none": 4},
        upsell={"high": 0, "medium": 0, "low": 0, "none": 10},
        avg_churn_risk=0.3,
        avg_upsell_score=None,
        by_channel=[{"channel": "voice", "churn_flags": 1, "upsell_flags": 0, "total": 5}],
    )
    assert sum(payload.churn.values()) == 10
    assert payload.avg_upsell_score is None
