"""Integration tests for POST /outcomes against a real (SQLite) DB.

Covers the paths that purely unit tests can't reach:

- Unique-constraint idempotency on ``(tenant_id, event_id)``.
- Dead-letter writes for ``interaction_not_found`` and future timestamps.
- HMAC signature flow end-to-end (401 when invalid, 202 when valid).
- Pydantic Literal enum rejection of unknown outcome types.
- Auto-fingerprint dedupe when ``event_id`` is omitted.

Tests use the ``test_client`` fixture from ``db_fixtures`` — no network
port bound, no external services.
"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


PREFIX = "/api/v1"


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ── Happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcomes_accepts_valid_payload_and_writes_to_features(
    test_client, test_interaction, test_session
):
    from backend.app.models import InteractionFeatures

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "customer_replied",
            "event_id": "evt-001",
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] == 1
    assert body["duplicate"] == 0
    assert body["dropped"] == 0

    # The proxy_outcomes JSONB got the event recorded.
    row = (
        await test_session.execute(
            select(InteractionFeatures).where(
                InteractionFeatures.interaction_id == test_interaction.id
            )
        )
    ).scalar_one()
    assert "customer_replied" in (row.proxy_outcomes or {})


# ── Idempotency ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcomes_idempotent_on_duplicate_event_id(
    test_client, test_interaction
):
    payload = {
        "interaction_id": str(test_interaction.id),
        "outcome_type": "customer_replied",
        "event_id": "evt-dup",
    }
    first = await test_client.post(f"{PREFIX}/outcomes", json=payload)
    second = await test_client.post(f"{PREFIX}/outcomes", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["accepted"] == 1
    assert second.json()["duplicate"] == 1
    assert second.json()["accepted"] == 0


@pytest.mark.asyncio
async def test_outcomes_auto_fingerprint_dedupes_event_id_less_calls(
    test_client, test_interaction
):
    payload = {
        "interaction_id": str(test_interaction.id),
        "outcome_type": "customer_replied",
        "occurred_at": "2026-04-17T10:00:00+00:00",
        # No event_id — handler fingerprints the payload.
    }
    first = await test_client.post(f"{PREFIX}/outcomes", json=payload)
    second = await test_client.post(f"{PREFIX}/outcomes", json=payload)
    assert first.json()["accepted"] == 1
    assert second.json()["duplicate"] == 1


# ── Dead-letter paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcomes_dead_letters_unknown_interaction(
    test_client, test_session
):
    from backend.app.models import DroppedOutcomeEvent

    unknown_id = str(uuid.uuid4())
    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": unknown_id,
            "outcome_type": "customer_replied",
            "event_id": "evt-unknown",
        },
    )
    assert resp.status_code == 202
    assert resp.json()["dropped"] == 1

    rows = (
        await test_session.execute(
            select(DroppedOutcomeEvent).where(
                DroppedOutcomeEvent.reason == "interaction_not_found"
            )
        )
    ).scalars().all()
    assert rows, "expected a dead-letter row for the unknown interaction"


@pytest.mark.asyncio
async def test_outcomes_dead_letters_future_timestamp(
    test_client, test_interaction, test_session
):
    from backend.app.models import DroppedOutcomeEvent

    far_future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "customer_replied",
            "occurred_at": far_future,
            "event_id": "evt-future",
        },
    )
    assert resp.status_code == 202
    assert resp.json()["dropped"] == 1

    reasons = [
        r.reason
        for r in (
            await test_session.execute(select(DroppedOutcomeEvent))
        ).scalars().all()
    ]
    assert "future_timestamp" in reasons


# ── HMAC ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcomes_rejects_unsigned_when_secret_set(
    test_client, test_interaction, test_tenant, test_session_factory
):
    # Install an HMAC secret on the tenant.
    from backend.app.models import Tenant

    async with test_session_factory() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.id == test_tenant.id))).scalar_one()
        tenant.outcomes_hmac_secret = "super-secret"
        await s.commit()

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "customer_replied",
            "event_id": "evt-unsigned",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_outcomes_accepts_signed_when_secret_set(
    test_client, test_interaction, test_tenant, test_session_factory
):
    from backend.app.models import Tenant

    secret = "super-secret-2"
    async with test_session_factory() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.id == test_tenant.id))).scalar_one()
        tenant.outcomes_hmac_secret = secret
        await s.commit()

    payload = {
        "interaction_id": str(test_interaction.id),
        "outcome_type": "customer_replied",
        "event_id": "evt-signed",
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Linda-Signature": _signature(secret, body),
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 1


# ── Literal enum ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outcomes_unknown_type_returns_422(
    test_client, test_interaction
):
    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "not_a_real_type",
            "event_id": "evt-badtype",
        },
    )
    # Pydantic rejects unknown Literal values with 422.
    assert resp.status_code == 422


# ── Dead-letter tail endpoint ───────────────────────────────────────────


# ── First-class customer_id ─────────────────────────────────────────────


async def _seed_customer(session_factory, tenant_id, *, attach_to=None):
    """Create a Customer; optionally set it as an interaction's resolved
    customer."""
    from backend.app.models import Customer, Interaction

    async with session_factory() as s:
        customer = Customer(tenant_id=tenant_id, name=f"Acme-{uuid.uuid4().hex[:6]}")
        s.add(customer)
        await s.commit()
        await s.refresh(customer)
        if attach_to is not None:
            interaction = (
                await s.execute(
                    select(Interaction).where(Interaction.id == attach_to)
                )
            ).scalar_one()
            interaction.customer_id = customer.id
            await s.commit()
        return customer


@pytest.mark.asyncio
async def test_outcomes_accepts_matching_customer_and_persists_it(
    test_client, test_interaction, test_tenant, test_session_factory, test_session
):
    from backend.app.models import OutcomeEventIngestion

    customer = await _seed_customer(
        test_session_factory, test_tenant.id, attach_to=test_interaction.id
    )

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "deal_won",
            "event_id": "evt-cust-match",
            "customer_id": str(customer.id),
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 1

    row = (
        await test_session.execute(
            select(OutcomeEventIngestion).where(
                OutcomeEventIngestion.event_id == "evt-cust-match"
            )
        )
    ).scalar_one()
    assert str(row.customer_id) == str(customer.id)


@pytest.mark.asyncio
async def test_outcomes_rejects_mismatched_customer_with_422(
    test_client, test_interaction, test_tenant, test_session_factory, test_session
):
    from backend.app.models import DroppedOutcomeEvent

    # The interaction resolves to customer A; the event claims customer B.
    await _seed_customer(
        test_session_factory, test_tenant.id, attach_to=test_interaction.id
    )
    other = await _seed_customer(test_session_factory, test_tenant.id)

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "deal_won",
            "event_id": "evt-cust-mismatch",
            "customer_id": str(other.id),
        },
    )
    assert resp.status_code == 422, resp.text
    assert "customer_mismatch" in resp.json()["detail"]

    # Dead-lettered (and the row survived the 422's rollback).
    reasons = [
        r.reason
        for r in (await test_session.execute(select(DroppedOutcomeEvent))).scalars().all()
    ]
    assert "customer_mismatch" in reasons


@pytest.mark.asyncio
async def test_outcomes_rejects_unknown_customer_with_422(
    test_client, test_interaction
):
    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "deal_won",
            "event_id": "evt-cust-unknown",
            "customer_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422, resp.text
    assert "customer_not_found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_outcomes_batch_drops_mismatch_without_failing_batch(
    test_client, test_interaction, test_tenant, test_session_factory
):
    await _seed_customer(
        test_session_factory, test_tenant.id, attach_to=test_interaction.id
    )
    other = await _seed_customer(test_session_factory, test_tenant.id)

    resp = await test_client.post(
        f"{PREFIX}/outcomes/batch",
        json={
            "events": [
                {
                    "interaction_id": str(test_interaction.id),
                    "outcome_type": "deal_won",
                    "event_id": "evt-batch-ok",
                },
                {
                    "interaction_id": str(test_interaction.id),
                    "outcome_type": "deal_lost",
                    "event_id": "evt-batch-bad",
                    "customer_id": str(other.id),
                },
            ]
        },
    )
    # Batch semantics unchanged: 202 with per-event counts.
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] == 1
    assert body["dropped"] == 1


@pytest.mark.asyncio
async def test_outcomes_accepts_claim_when_interaction_has_no_resolved_customer(
    test_client, test_interaction, test_tenant, test_session_factory
):
    # No customer attached to the interaction — the claim can't be
    # disproven, so it's accepted and persisted.
    customer = await _seed_customer(test_session_factory, test_tenant.id)

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "customer_replied",
            "event_id": "evt-cust-unresolved",
            "customer_id": str(customer.id),
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 1


# ── Naive occurred_at normalization (item 6) ────────────────────────────


@pytest.mark.asyncio
async def test_outcomes_naive_occurred_at_is_normalized_to_utc(
    test_client, test_interaction, test_session
):
    """A naive occurred_at used to hit an aware/naive TypeError in
    _apply_events; it is now treated as UTC and serialized with an
    explicit offset."""
    from backend.app.models import InteractionFeatures

    resp = await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": str(test_interaction.id),
            "outcome_type": "customer_replied",
            "event_id": "evt-naive-ts",
            "occurred_at": "2026-07-01T10:00:00",  # no offset
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 1

    row = (
        await test_session.execute(
            select(InteractionFeatures).where(
                InteractionFeatures.interaction_id == test_interaction.id
            )
        )
    ).scalar_one()
    recorded = row.proxy_outcomes["customer_replied"]
    entry = recorded[0] if isinstance(recorded, list) else recorded
    assert entry["occurred_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_dead_letter_recent_returns_recent_drops(
    test_client, test_interaction
):
    # Force one drop then tail the endpoint.
    unknown_id = str(uuid.uuid4())
    await test_client.post(
        f"{PREFIX}/outcomes",
        json={
            "interaction_id": unknown_id,
            "outcome_type": "customer_replied",
            "event_id": "evt-deadtail",
        },
    )
    resp = await test_client.get(f"{PREFIX}/outcomes/dead-letter/recent?limit=10")
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["reason"] == "interaction_not_found" for r in rows)
