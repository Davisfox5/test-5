"""Lifecycle classification + churn gating on outbound webhook payloads.

A prospect/lead has no relationship to churn out of, so churn fields must
be absent (None) on EVERY outbound surface for prospect accounts — the
REST serializers already gate on lifecycle; these tests pin the shared
classifier and the webhook-summary gate so the leak fixed for the Flex
console can't regress.
"""

from types import SimpleNamespace
from typing import Optional
from unittest import mock

from backend.app.services.lifecycle import is_client, lifecycle_stage


def _customer(renewal_date=None, onboarding_status=None):
    return SimpleNamespace(
        renewal_date=renewal_date, onboarding_status=onboarding_status
    )


def test_lifecycle_stage_prospect_by_default():
    assert lifecycle_stage(_customer()) == "prospect"
    assert is_client(_customer()) is False


def test_lifecycle_stage_missing_customer_is_prospect():
    assert lifecycle_stage(None) == "prospect"
    assert is_client(None) is False


def test_lifecycle_stage_client_on_renewal_date():
    assert lifecycle_stage(_customer(renewal_date="2027-01-01")) == "client"


def test_lifecycle_stage_client_on_onboarding_status():
    assert lifecycle_stage(_customer(onboarding_status="in_progress")) == "client"


def test_contacts_serializer_uses_shared_classifier():
    from backend.app.api import contacts

    assert contacts._lifecycle_stage is lifecycle_stage


class _CapturedEmit:
    def __init__(self):
        self.calls = []

    async def __call__(self, db, tenant_id, event, payload):
        self.calls.append((event, payload))


def _run_emit(account_lifecycle_stage: str, outcome_type: Optional[str] = None):
    """Drive _emit_webhooks_for_interaction with emit_event captured."""
    import uuid

    from backend.app import tasks

    captured = _CapturedEmit()

    class _FakeSessionCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    with mock.patch("backend.app.db.async_session", return_value=_FakeSessionCtx()), \
         mock.patch(
             "backend.app.services.webhook_dispatcher.emit_event", captured
         ):
        tasks._emit_webhooks_for_interaction(
            tenant_id=uuid.uuid4(),
            interaction_id=uuid.uuid4(),
            insights={
                "summary": "great call",
                "sentiment_overall": "positive",
                "sentiment_score": 8.5,
                "churn_risk_signal": "none",
                "upsell_signal": "medium",
            },
            outcome_type=outcome_type,
            outcome_confidence=0.9 if outcome_type else None,
            account_lifecycle_stage=account_lifecycle_stage,
        )
    return captured.calls


def test_webhook_summary_gates_churn_for_prospect():
    calls = _run_emit("prospect")
    assert calls, "interaction.analyzed should still emit"
    event, payload = calls[0]
    assert event == "interaction.analyzed"
    assert payload["churn_risk_signal"] is None
    assert payload["lifecycle_stage"] == "prospect"
    # Sentiment is meaningful for any account — never gated.
    assert payload["sentiment_score"] == 8.5


def test_webhook_summary_keeps_churn_for_client():
    calls = _run_emit("client")
    event, payload = calls[0]
    assert payload["churn_risk_signal"] == "none"
    assert payload["lifecycle_stage"] == "client"


def test_outcome_inferred_payload_inherits_gate():
    calls = _run_emit("prospect", outcome_type="won")
    events = {e: p for e, p in calls}
    assert "interaction.outcome_inferred" in events
    assert events["interaction.outcome_inferred"]["churn_risk_signal"] is None
