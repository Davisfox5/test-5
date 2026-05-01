"""Pipedrive write-back transient-failure tests.

Companion to ``test_pipedrive_writeback`` (happy path) — covers
401-refresh, 5xx-retry, and permanent-fail-after-3-tries paths.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import CrmAuthError, CrmError, CrmTransientError
from backend.app.services.crm.pipedrive import PipedriveAdapter


def _mock_response(status: int, body: dict):
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        content=json.dumps(body).encode(),
        json=lambda: body,
    )


@pytest.fixture
def adapter():
    a = PipedriveAdapter(
        access_token="tok",
        api_domain="https://foo.pipedrive.com",
        refresh_token="ref",
    )

    async def _noop(_):
        return None

    a._sleep = _noop
    return a


# ── 401 → refresh + retry ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_refreshes_on_401(adapter, monkeypatch):
    from backend.app.services.crm import pipedrive as pd_module

    monkeypatch.setattr(
        pd_module,
        "get_settings",
        lambda: SimpleNamespace(
            PIPEDRIVE_CLIENT_ID="cid", PIPEDRIVE_CLIENT_SECRET="csec"
        ),
    )

    # Refresh-token endpoint (called via client.post)
    adapter._client.post = AsyncMock(
        return_value=_mock_response(
            200,
            {
                "access_token": "new-at",
                "refresh_token": "new-rt",
                "expires_in": 3600,
                "api_domain": "https://foo.pipedrive.com",
            },
        )
    )

    # Activity POST: 401 then 200
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(401, {"error": "Unauthorized"}),
            _mock_response(200, {"data": {"id": 99}, "success": True}),
        ]
    )

    act_id = await adapter.create_activity(
        subject="Follow up", activity_type="task"
    )
    assert act_id == "99"
    # Refresh hit oauth.pipedrive.com once
    assert adapter._client.post.await_count == 1
    refresh_call = adapter._client.post.await_args
    assert "oauth.pipedrive.com/oauth/token" in refresh_call.args[0]
    # Token rotated in memory
    assert adapter._access_token == "new-at"
    assert adapter._refresh_token == "new-rt"


@pytest.mark.asyncio
async def test_create_activity_persistent_401_raises_auth_error(
    adapter, monkeypatch
):
    from backend.app.services.crm import pipedrive as pd_module

    monkeypatch.setattr(
        pd_module,
        "get_settings",
        lambda: SimpleNamespace(
            PIPEDRIVE_CLIENT_ID="cid", PIPEDRIVE_CLIENT_SECRET="csec"
        ),
    )

    adapter._client.post = AsyncMock(
        return_value=_mock_response(200, {"access_token": "still-bad"})
    )
    adapter._client.request = AsyncMock(
        return_value=_mock_response(401, {})
    )

    with pytest.raises(CrmAuthError):
        await adapter.create_activity(subject="X", activity_type="task")


# ── 5xx retry path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_note_retries_5xx_then_succeeds(adapter):
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(502, {"error": "bad gateway"}),
            _mock_response(503, {"error": "service unavailable"}),
            _mock_response(200, {"data": {"id": 777}, "success": True}),
        ]
    )

    note_id = await adapter.create_note(
        content="Summary", deal_external_id="55"
    )
    assert note_id == "777"
    assert adapter._client.request.await_count == 3


@pytest.mark.asyncio
async def test_create_activity_fails_after_three_5xx(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(500, {"error": "down"})
    )

    with pytest.raises(CrmTransientError):
        await adapter.create_activity(subject="X", activity_type="task")
    assert adapter._client.request.await_count == 3


@pytest.mark.asyncio
async def test_update_deal_stage_retries_5xx_then_fails(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(503, {"error": "down"})
    )

    with pytest.raises(CrmTransientError):
        await adapter.update_deal_stage(
            deal_external_id="55", stage_external_id="4"
        )
    assert adapter._client.request.await_count == 3


# ── Permanent 4xx → no retry ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_does_not_retry_400(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(400, {"error": "bad payload"})
    )

    with pytest.raises(CrmError) as exc:
        await adapter.create_activity(subject="X", activity_type="task")
    assert not isinstance(exc.value, CrmTransientError)
    assert adapter._client.request.await_count == 1


# ── Rate limit (429) is treated as transient ─────────────────────


@pytest.mark.asyncio
async def test_create_note_retries_429_then_succeeds(adapter):
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(429, {"error": "rate"}),
            _mock_response(200, {"data": {"id": 12}, "success": True}),
        ]
    )

    note_id = await adapter.create_note(
        content="Summary", deal_external_id="55"
    )
    assert note_id == "12"
    assert adapter._client.request.await_count == 2


# ── Request shape after retry ───────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_request_shape_after_retry(adapter):
    adapter._client.request = AsyncMock(
        side_effect=[
            _mock_response(502, {}),
            _mock_response(200, {"data": {"id": 1}, "success": True}),
        ]
    )

    await adapter.create_activity(
        subject="Call back",
        activity_type="task",
        due_date="2026-05-02",
        deal_external_id="55",
        contact_external_id="10",
    )
    calls = adapter._client.request.await_args_list
    assert len(calls) == 2
    for c in calls:
        assert c.args[0] == "POST"
        assert c.args[1].endswith("/v1/activities")
        body = c.kwargs["json"]
        assert body["subject"] == "Call back"
        assert body["due_date"] == "2026-05-02"
        assert body["deal_id"] == 55
        assert body["person_id"] == 10
