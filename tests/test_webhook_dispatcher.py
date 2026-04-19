"""Tests for the outbound webhook dispatcher.

Covers the pure pieces — HMAC signing, event-name matching — plus the
``deliver_one`` retry loop with mocked HTTP responses and a FakeSession
that captures row mutations.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.webhook_dispatcher import (
    _BACKOFF_SECONDS,
    _MAX_ATTEMPTS,
    _event_matches,
    deliver_one,
    sign_payload,
)
from backend.app.services.webhook_events import (
    BRIEF_ALERT_EVENT_MAP,
    CUSTOMER_OUTCOME_EVENT_MAP,
    WEBHOOK_EVENTS,
    is_known_event,
)


# ── Pure helpers ────────────────────────────────────────────────────


def test_sign_payload_hmac_sha256_hex():
    sig = sign_payload("hello", "s3cret")
    # Known HMAC-SHA256 hex for ("s3cret", "hello")
    import hashlib
    import hmac
    expected = hmac.new(b"s3cret", b"hello", hashlib.sha256).hexdigest()
    assert sig == expected
    # Stable (deterministic) across calls.
    assert sign_payload("hello", "s3cret") == sig


def test_event_matches_wildcard():
    assert _event_matches(["*"], "customer.churned") is True
    assert _event_matches(["*"], "anything.at.all") is True


def test_event_matches_exact():
    assert _event_matches(["customer.churned"], "customer.churned") is True
    assert _event_matches(["customer.churned"], "customer.upsold") is False


def test_event_matches_prefix():
    assert _event_matches(["customer.*"], "customer.churned") is True
    assert _event_matches(["customer.*"], "interaction.analyzed") is False
    # "brief_alert.*" shouldn't match just "brief_alert" on its own.
    assert _event_matches(["brief_alert.*"], "brief_alert") is False
    assert _event_matches(["brief_alert.*"], "brief_alert.churn") is True


def test_event_matches_empty_list_is_false():
    assert _event_matches([], "anything") is False


def test_event_catalog_sanity():
    # Every event in the maps we ship to callers must exist in the catalog.
    for k in CUSTOMER_OUTCOME_EVENT_MAP.values():
        assert k in WEBHOOK_EVENTS, f"{k} missing from catalog"
    for k in BRIEF_ALERT_EVENT_MAP.values():
        assert k in WEBHOOK_EVENTS, f"{k} missing from catalog"
    # is_known_event accepts wildcards + every catalog key.
    assert is_known_event("*")
    assert is_known_event("customer.churned")
    assert not is_known_event("does.not.exist")


# ── deliver_one retry loop ──────────────────────────────────────────


class FakeWebhook:
    def __init__(self, *, url="https://example.com/hook", secret="secret-x"):
        self.id = uuid.uuid4()
        self.url = url
        self.secret = secret
        self.active = True
        self.last_delivered_at = None
        self.last_failure_at = None
        self.consecutive_failures = 0


class FakeDelivery:
    def __init__(self, webhook: FakeWebhook):
        self.id = uuid.uuid4()
        self.webhook_id = webhook.id
        self.tenant_id = uuid.uuid4()
        self.event = "interaction.analyzed"
        self.payload = {"event": "interaction.analyzed", "data": {"x": 1}}
        self.status = "pending"
        self.attempts = []
        self.attempt_count = 0
        self.last_status_code = None
        self.last_error = None
        self.next_retry_at = None
        self.delivered_at = None


class FakeSession:
    """Async-session double that resolves ``db.get`` via an internal map."""

    def __init__(self, webhook: FakeWebhook, delivery: FakeDelivery) -> None:
        self._map = {
            (FakeWebhook, webhook.id): webhook,
            (FakeDelivery, delivery.id): delivery,
        }

    async def get(self, model, key):
        # The real dispatcher passes SQLAlchemy model classes; we accept
        # both real and fake classes by matching the type name.
        for (cls, k), v in self._map.items():
            if cls.__name__.endswith(model.__name__) and k == key:
                return v
        return None


@pytest.fixture(autouse=True)
def _patch_model_classes(monkeypatch):
    """Make ``db.get(Webhook, id)`` inside deliver_one resolve to our fakes."""
    import backend.app.services.webhook_dispatcher as mod

    monkeypatch.setattr(mod, "Webhook", FakeWebhook, raising=True)
    monkeypatch.setattr(mod, "WebhookDelivery", FakeDelivery, raising=True)
    # Prevent scheduling a real Celery retry.
    monkeypatch.setattr(
        "backend.app.tasks.webhook_deliver",
        SimpleNamespace(
            apply_async=lambda *a, **k: None,
            delay=lambda *a, **k: None,
        ),
        raising=False,
    )


def _mock_httpx_client(status_code: int = 200):
    """Patch httpx.AsyncClient in the dispatcher module to return a canned
    response. We mock the context manager shape httpx exposes."""
    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            return SimpleNamespace(status_code=status_code)

    return _Client


@pytest.mark.asyncio
async def test_deliver_one_success_marks_sent_and_clears_failures():
    wh = FakeWebhook()
    wh.consecutive_failures = 3  # stale failure count
    d = FakeDelivery(wh)
    db = FakeSession(wh, d)

    with patch(
        "backend.app.services.webhook_dispatcher.httpx.AsyncClient",
        _mock_httpx_client(202),
    ):
        result = await deliver_one(db, d.id)

    assert result["status"] == "sent"
    assert result["status_code"] == 202
    assert d.status == "sent"
    assert d.attempt_count == 1
    assert d.delivered_at is not None
    assert wh.last_delivered_at is not None
    # Counter resets on success.
    assert wh.consecutive_failures == 0


@pytest.mark.asyncio
async def test_deliver_one_5xx_schedules_retry_with_backoff():
    wh = FakeWebhook()
    d = FakeDelivery(wh)
    db = FakeSession(wh, d)

    with patch(
        "backend.app.services.webhook_dispatcher.httpx.AsyncClient",
        _mock_httpx_client(503),
    ):
        result = await deliver_one(db, d.id)

    assert result["status"] == "retrying"
    assert d.status == "pending"
    assert d.attempt_count == 1
    # Backoff: first retry uses the second element of the schedule.
    assert result["next_retry_in"] == _BACKOFF_SECONDS[1]
    assert d.next_retry_at is not None
    assert wh.consecutive_failures == 1


@pytest.mark.asyncio
async def test_deliver_one_hits_max_attempts_becomes_dead_letter():
    wh = FakeWebhook()
    d = FakeDelivery(wh)
    d.attempt_count = _MAX_ATTEMPTS - 1  # one more failure kills it
    db = FakeSession(wh, d)

    with patch(
        "backend.app.services.webhook_dispatcher.httpx.AsyncClient",
        _mock_httpx_client(500),
    ):
        result = await deliver_one(db, d.id)

    assert result["status"] == "dead_letter"
    assert d.status == "dead_letter"
    assert d.next_retry_at is None
    assert wh.consecutive_failures == 1  # incremented once from this attempt


@pytest.mark.asyncio
async def test_deliver_one_skips_disabled_webhook():
    wh = FakeWebhook()
    wh.active = False
    d = FakeDelivery(wh)
    db = FakeSession(wh, d)

    result = await deliver_one(db, d.id)
    assert result["status"] == "dead_letter"
    assert d.status == "dead_letter"
    assert "missing or disabled" in (d.last_error or "")


@pytest.mark.asyncio
async def test_deliver_one_noop_when_already_sent():
    wh = FakeWebhook()
    d = FakeDelivery(wh)
    d.status = "sent"
    db = FakeSession(wh, d)
    result = await deliver_one(db, d.id)
    assert result["status"] == "sent"
    # No side-effects on a second call.
    assert d.attempt_count == 0
