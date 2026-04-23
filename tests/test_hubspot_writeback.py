"""HubSpot write-back tests — deals iteration + notes + tasks + stage update.

Mocks the httpx client like the read-path tests do. Asserts the
adapter builds the right JSON payloads and parses the right ids out
of HubSpot's v3 responses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import CrmError
from backend.app.services.crm.hubspot import HubSpotAdapter


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
    return HubSpotAdapter(
        access_token="tok",
        refresh_token="ref",
        field_map={"lead_temperature": "cf_lead_temp"},
    )


# ── Deals ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_iter_deals_parses_hubspot_shape(adapter):
    body = {
        "results": [
            {
                "id": "123",
                "properties": {
                    "dealname": "Acme expansion",
                    "dealstage": "qualifiedtobuy",
                    "pipeline": "default",
                    "amount": "15000",
                    "hs_deal_stage_probability": "0.4",
                    "closedate": "2026-08-01",
                    "hubspot_owner_id": "42",
                },
                "associations": {
                    "companies": {"results": [{"id": "9001", "type": "deal_to_company"}]},
                    "contacts": {"results": [{"id": "500"}]},
                },
            }
        ]
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert len(deals) == 1
    d = deals[0]
    assert d.external_id == "123"
    assert d.title == "Acme expansion"
    assert d.stage == "qualifiedtobuy"
    assert d.status == "open"
    assert d.amount == 15000.0
    assert d.probability == 0.4
    assert d.close_date == "2026-08-01"
    assert d.customer_external_id == "9001"
    assert d.contact_external_id == "500"


@pytest.mark.asyncio
async def test_iter_deals_status_won_for_closedwon_stage(adapter):
    body = {
        "results": [
            {"id": "1", "properties": {"dealname": "X", "dealstage": "closedwon"}}
        ]
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert deals[0].status == "won"


@pytest.mark.asyncio
async def test_iter_deals_status_lost_for_closedlost_stage(adapter):
    body = {
        "results": [
            {"id": "1", "properties": {"dealname": "X", "dealstage": "closedlost"}}
        ]
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert deals[0].status == "lost"


# ── Notes write-back ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_note_builds_associations_for_all_targets(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"id": "note-777"})
    )
    note_id = await adapter.create_note(
        content="Summary",
        deal_external_id="d1",
        contact_external_id="c1",
        customer_external_id="co1",
    )
    assert note_id == "note-777"
    body = adapter._client.request.await_args.kwargs["json"]
    associations = body["associations"]
    target_ids = {a["to"]["id"] for a in associations}
    assert target_ids == {"d1", "c1", "co1"}
    # Each association carries a HubSpot-defined type id.
    for a in associations:
        assert a["types"][0]["associationCategory"] == "HUBSPOT_DEFINED"
        assert isinstance(a["types"][0]["associationTypeId"], int)


@pytest.mark.asyncio
async def test_create_note_requires_a_target(adapter):
    with pytest.raises(CrmError):
        await adapter.create_note(content="Summary")


@pytest.mark.asyncio
async def test_create_note_rejects_empty(adapter):
    with pytest.raises(CrmError):
        await adapter.create_note(content="", deal_external_id="d1")


# ── Activities write-back ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_sends_task_fields(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"id": "task-42"})
    )
    act_id = await adapter.create_activity(
        subject="Follow up",
        activity_type="task",
        due_date="2026-05-01",
        note="Call back Friday",
        deal_external_id="d1",
        contact_external_id="c1",
    )
    assert act_id == "task-42"
    body = adapter._client.request.await_args.kwargs["json"]
    props = body["properties"]
    assert props["hs_task_subject"] == "Follow up"
    assert props["hs_task_status"] == "NOT_STARTED"
    assert props["hs_task_type"] == "TODO"
    assert "hs_task_due_date" in props  # epoch-millis string
    assert int(props["hs_task_due_date"]) > 0
    # Both associations should have landed.
    assoc_ids = {a["to"]["id"] for a in body["associations"]}
    assert assoc_ids == {"d1", "c1"}


@pytest.mark.asyncio
async def test_create_activity_maps_call_type(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"id": "task-1"})
    )
    await adapter.create_activity(subject="Dial them", activity_type="call")
    body = adapter._client.request.await_args.kwargs["json"]
    assert body["properties"]["hs_task_type"] == "CALL"


# ── Deal stage update ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_deal_stage_patches_dealstage(adapter):
    adapter._client.request = AsyncMock(return_value=_mock_response(200, {}))
    await adapter.update_deal_stage(
        deal_external_id="123", stage_external_id="closedwon"
    )
    call = adapter._client.request.await_args
    assert call.args[0] == "PATCH"
    assert call.args[1].endswith("/crm/v3/objects/deals/123")
    assert call.kwargs["json"] == {"properties": {"dealstage": "closedwon"}}
