"""Salesforce write-back transient-failure tests.

Covers what the existing happy-path suite (``test_salesforce_writeback``)
doesn't:

- 401 → refresh access_token via the stored refresh_token, then retry
- 5xx → retry with exponential backoff up to 3 attempts
- 5xx that never recovers → permanent fail after 3 attempts
- Permanent 4xx is not retried
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import CrmAuthError, CrmError, CrmTransientError
from backend.app.services.crm.salesforce import SalesforceAdapter


def _mock_response(status: int, body: dict):
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        content=json.dumps(body).encode(),
        json=lambda: body,
    )


@pytest.fixture
def adapter():
    a = SalesforceAdapter(
        access_token="tok",
        instance_url="https://acme.my.salesforce.com",
        refresh_token="ref",
    )

    # No real sleeps in tests.
    async def _noop(_):
        return None

    a._sleep = _noop
    return a


# ── 401 → refresh + retry ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_refreshes_access_token_on_401(adapter, monkeypatch):
    """First Task POST returns 401; the adapter calls the OAuth refresh
    endpoint and then re-issues the POST with the new access token."""

    # Stub the get_settings call inside _refresh_access_token so the
    # client_id/secret guard passes.
    from backend.app.services.crm import salesforce as sf_module

    monkeypatch.setattr(
        sf_module,
        "get_settings",
        lambda: SimpleNamespace(
            SALESFORCE_CLIENT_ID="cid", SALESFORCE_CLIENT_SECRET="csec"
        ),
    )

    # A separate mock for the refresh-token POST (called via client.post,
    # not client.request).
    adapter._client.post = AsyncMock(
        return_value=_mock_response(
            200,
            {
                "access_token": "new-at",
                "instance_url": "https://acme.my.salesforce.com",
            },
        )
    )

    # Sequence of request() calls:
    #   1. POST /Task → 401 (token expired)
    #   2. POST /Task → 201 (after refresh)
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(401, {"error": "invalid_session"}),
            _mock_response(201, {"id": "00Tnew"}),
        ]
    )

    task_id = await adapter.create_activity(
        subject="Follow up", activity_type="Call"
    )
    assert task_id == "00Tnew"
    # Refresh was hit exactly once
    assert adapter._client.post.await_count == 1
    refresh_call = adapter._client.post.await_args
    assert "/services/oauth2/token" in refresh_call.args[0]
    assert refresh_call.kwargs["data"]["grant_type"] == "refresh_token"
    # And the access token in memory was rotated
    assert adapter._access_token == "new-at"


@pytest.mark.asyncio
async def test_create_activity_raises_auth_error_after_persistent_401(
    adapter, monkeypatch
):
    from backend.app.services.crm import salesforce as sf_module

    monkeypatch.setattr(
        sf_module,
        "get_settings",
        lambda: SimpleNamespace(
            SALESFORCE_CLIENT_ID="cid", SALESFORCE_CLIENT_SECRET="csec"
        ),
    )

    adapter._client.post = AsyncMock(
        return_value=_mock_response(200, {"access_token": "still-bad"})
    )
    adapter._client.request = AsyncMock(
        return_value=_mock_response(401, {"error": "invalid_session"})
    )

    with pytest.raises(CrmAuthError):
        await adapter.create_activity(subject="X", activity_type="Call")


# ── 5xx → retry with backoff, then succeed ────────────────────────


@pytest.mark.asyncio
async def test_create_activity_retries_5xx_then_succeeds(adapter):
    """Two 503s followed by a 201 — adapter should land on the third try."""
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(503, {"error": "service unavailable"}),
            _mock_response(503, {"error": "service unavailable"}),
            _mock_response(201, {"id": "00Tok"}),
        ]
    )

    task_id = await adapter.create_activity(subject="X", activity_type="Call")
    assert task_id == "00Tok"
    assert adapter._client.request.await_count == 3


@pytest.mark.asyncio
async def test_create_note_retries_5xx_each_call_independently(adapter):
    """ContentNote create + Link both succeed via retry on 5xx."""
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(503, {}),  # ContentNote attempt 1
            _mock_response(201, {"id": "069note"}),  # ContentNote attempt 2
            _mock_response(201, {"id": "06Alink"}),  # Link attempt 1
        ]
    )

    note_id = await adapter.create_note(
        content="Summary", deal_external_id="006abc"
    )
    assert note_id == "069note"


# ── 5xx that never recovers → permanent fail after 3 tries ──────


@pytest.mark.asyncio
async def test_create_activity_fails_after_three_5xx(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(503, {"error": "still down"})
    )

    with pytest.raises(CrmTransientError):
        await adapter.create_activity(subject="X", activity_type="Call")
    # Default max_attempts=3 — exactly three POSTs.
    assert adapter._client.request.await_count == 3


@pytest.mark.asyncio
async def test_update_deal_stage_retries_5xx_then_fails(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(500, {"error": "boom"})
    )

    with pytest.raises(CrmTransientError):
        await adapter.update_deal_stage(
            deal_external_id="006abc", stage_external_id="Closed Won"
        )
    assert adapter._client.request.await_count == 3


# ── Permanent 4xx → no retry ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_does_not_retry_400(adapter):
    """A 400 (bad payload) is permanent — retry would just waste cycles."""
    adapter._client.request = AsyncMock(
        return_value=_mock_response(400, {"error": "bad request"})
    )

    with pytest.raises(CrmError) as exc:
        await adapter.create_activity(subject="X", activity_type="Call")
    # Not a transient error
    assert not isinstance(exc.value, CrmTransientError)
    assert adapter._client.request.await_count == 1


# ── Request shape sanity check ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_request_shape_after_retry(adapter):
    """When we retry, the second attempt sends the same payload + URL."""
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(502, {}),
            _mock_response(201, {"id": "00Tok"}),
        ]
    )

    await adapter.create_activity(
        subject="Call back",
        activity_type="Call",
        due_date="2026-05-02",
        deal_external_id="006abc",
        contact_external_id="003c",
    )
    # Both attempts should have been POSTs to /sobjects/Task with the
    # same payload — no payload mutation between retries.
    calls = adapter._client.request.await_args_list
    assert len(calls) == 2
    for c in calls:
        assert c.args[0] == "POST"
        assert c.args[1].endswith("/sobjects/Task")
        body = c.kwargs["json"]
        assert body["Subject"] == "Call back"
        assert body["WhatId"] == "006abc"
        assert body["WhoId"] == "003c"
