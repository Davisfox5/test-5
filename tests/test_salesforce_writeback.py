"""Salesforce write-back tests — opportunity iteration + ContentNote +
Task create + stage update.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import CrmError
from backend.app.services.crm.salesforce import SalesforceAdapter


def _mock_response(status: int, body: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        content=json.dumps(body).encode(),
        json=lambda: body,
    )


async def _collect(iterable):
    out = []
    async for item in iterable:
        out.append(item)
    return out


@pytest.fixture
def adapter():
    return SalesforceAdapter(
        access_token="tok",
        instance_url="https://acme.my.salesforce.com",
        refresh_token="ref",
        field_map={"call_outcome": "Call_Outcome__c"},
    )


# ── Deals (opportunities) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_iter_deals_maps_open_status(adapter):
    body = {
        "records": [
            {
                "Id": "006abc",
                "Name": "Acme renewal",
                "StageName": "Negotiation",
                "Amount": 50000,
                "Probability": 75,
                "CloseDate": "2026-07-15",
                "IsClosed": False,
                "IsWon": False,
                "AccountId": "001xyz",
                "OwnerId": "005owner",
            }
        ],
        "done": True,
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert len(deals) == 1
    d = deals[0]
    assert d.external_id == "006abc"
    assert d.title == "Acme renewal"
    assert d.stage == "Negotiation"
    assert d.status == "open"
    assert d.amount == 50000.0
    assert d.probability == 75.0
    assert d.close_date == "2026-07-15"
    assert d.customer_external_id == "001xyz"


@pytest.mark.asyncio
async def test_iter_deals_status_won(adapter):
    body = {
        "records": [
            {"Id": "1", "Name": "X", "IsClosed": True, "IsWon": True}
        ],
        "done": True,
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert deals[0].status == "won"


@pytest.mark.asyncio
async def test_iter_deals_status_lost(adapter):
    body = {
        "records": [
            {"Id": "1", "Name": "X", "IsClosed": True, "IsWon": False}
        ],
        "done": True,
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert deals[0].status == "lost"


# ── ContentNote write-back ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_note_posts_content_note_then_link(adapter):
    """Salesforce needs two calls: create the ContentNote, then link it
    to its parent via ContentDocumentLink."""
    responses = [
        _mock_response(201, {"id": "069note"}),  # ContentNote create
        _mock_response(201, {"id": "06Alink"}),  # ContentDocumentLink create
    ]
    adapter._client.request = AsyncMock(side_effect=responses)
    note_id = await adapter.create_note(
        content="Summary of the call\nSecond line",
        deal_external_id="006abc",
    )
    assert note_id == "069note"
    calls = adapter._client.request.await_args_list
    # First call: create ContentNote
    note_body = calls[0].kwargs["json"]
    assert note_body["Title"] == "Summary of the call"  # first line only
    # Content is base64-encoded HTML; decoding should give back the text.
    decoded = base64.b64decode(note_body["Content"]).decode("utf-8")
    assert "Summary of the call" in decoded
    # Second call: link to parent
    link_body = calls[1].kwargs["json"]
    assert link_body["ContentDocumentId"] == "069note"
    assert link_body["LinkedEntityId"] == "006abc"
    assert link_body["ShareType"] == "V"


@pytest.mark.asyncio
async def test_create_note_requires_parent(adapter):
    with pytest.raises(CrmError):
        await adapter.create_note(content="Orphan note")


@pytest.mark.asyncio
async def test_create_note_rejects_empty(adapter):
    with pytest.raises(CrmError):
        await adapter.create_note(content="", deal_external_id="006")


# ── Task write-back ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_builds_task_record(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(201, {"id": "00Ttask"})
    )
    task_id = await adapter.create_activity(
        subject="Call back Friday",
        activity_type="Call",
        due_date="2026-05-02",
        note="Pricing follow-up",
        deal_external_id="006abc",
        contact_external_id="003contact",
    )
    assert task_id == "00Ttask"
    body = adapter._client.request.await_args.kwargs["json"]
    assert body["Subject"] == "Call back Friday"
    assert body["Type"] == "Call"
    assert body["ActivityDate"] == "2026-05-02"
    assert body["Description"] == "Pricing follow-up"
    assert body["WhatId"] == "006abc"
    assert body["WhoId"] == "003contact"


# ── Stage update ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_deal_stage_patches_opportunity(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(204, {})
    )
    await adapter.update_deal_stage(
        deal_external_id="006abc", stage_external_id="Closed Won"
    )
    call = adapter._client.request.await_args
    assert call.args[0] == "PATCH"
    assert call.args[1].endswith("/sobjects/Opportunity/006abc")
    assert call.kwargs["json"] == {"StageName": "Closed Won"}
