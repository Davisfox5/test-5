"""Unit tests for outcome inference — maps AI insights to outcome labels."""

from backend.app.services.kb.outcome_inference import infer_outcome


def test_closed_won_detected_from_summary():
    insights = {
        "summary": "Great call! They signed the contract on the spot.",
        "sentiment_overall": "positive",
        "sentiment_score": 9.2,
        "churn_risk_signal": "none",
        "upsell_signal": "none",
    }
    out = infer_outcome(insights)
    assert out.outcome_type == "closed_won"
    assert out.outcome_confidence > 0.7
    assert any(e["event_type"] == "became_customer" for e in out.customer_events)


def test_closed_lost_detected():
    insights = {
        "summary": "They went with competitor at the last minute.",
        "sentiment_overall": "negative",
        "sentiment_score": 3,
        "churn_risk_signal": "none",
        "upsell_signal": "none",
    }
    out = infer_outcome(insights)
    assert out.outcome_type == "closed_lost"
    assert any(e["event_type"] == "churned" for e in out.customer_events)


def test_at_risk_flagged_on_high_churn():
    insights = {
        "summary": "Customer complained about the last outage.",
        "sentiment_overall": "negative",
        "churn_risk_signal": "high",
        "churn_risk": 0.85,
        "upsell_signal": "none",
    }
    out = infer_outcome(insights)
    assert any(e["event_type"] == "at_risk_flagged" for e in out.customer_events)


def test_upsell_signal_emits_advocate_event():
    insights = {
        "summary": "They asked about the enterprise tier and more seats.",
        "sentiment_overall": "positive",
        "upsell_signal": "high",
        "upsell_score": 0.9,
        "churn_risk_signal": "none",
    }
    out = infer_outcome(insights)
    # Explicit disposition short-circuits to the upsell path
    assert out.outcome_type in ("upsell_opportunity", "info_shared")
    assert any(e["event_type"] == "advocate_signal" for e in out.customer_events)


def test_no_signal_falls_back_to_sentiment_bucket():
    insights = {
        "summary": "Quick check-in call, nothing special.",
        "sentiment_overall": "neutral",
        "sentiment_score": 5.5,
        "churn_risk_signal": "none",
        "upsell_signal": "none",
    }
    out = infer_outcome(insights)
    assert out.outcome_type == "no_decision"
    assert out.customer_events == []


def test_action_item_disposition_detected():
    insights = {
        "summary": "Good call.",
        "action_items": [{"title": "Demo scheduled for next Tuesday", "priority": "high"}],
        "sentiment_overall": "positive",
        "churn_risk_signal": "none",
        "upsell_signal": "none",
    }
    out = infer_outcome(insights)
    assert out.outcome_type == "demo_scheduled"
