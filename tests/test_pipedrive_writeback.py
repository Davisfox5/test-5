"""Pipedrive write-back tests — deals iter, notes, activities, field map.

Same approach as the existing Pipedrive adapter tests: mock the httpx
client, assert the adapter produces the right API calls and parses
the responses correctly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import (
    CrmCapabilityMissing,
    CrmError,
)
from backend.app.services.crm.pipedrive import PipedriveAdapter


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
    return PipedriveAdapter(
        access_token="tok",
        api_domain="https://foo.pipedrive.com",
        refresh_token="refresh",
        field_map={"call_outcome": "cf_outcome_hash", "lead_temp": "cf_temp_hash"},
    )


# ── Deals iteration ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_iter_deals_parses_pipedrive_shape(adapter):
    body = {
        "data": [
            {
                "id": 55,
                "title": "Acme expansion",
                "stage_id": 3,
                "status": "open",
                "value": 12500.0,
                "currency": "USD",
                "probability": 60,
                "expected_close_date": "2026-06-01",
                "org_id": {"value": 99, "name": "Acme"},
                "person_id": {"value": 10, "name": "Sarah"},
                "owner_id": {"value": 1, "name": "Rep A"},
                "pipeline_id": 1,
            }
        ],
        "additional_data": {"pagination": {"more_items_in_collection": False}},
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    assert len(deals) == 1
    d = deals[0]
    assert d.external_id == "55"
    assert d.title == "Acme expansion"
    assert d.stage == "3"
    assert d.status == "open"
    assert d.amount == 12500.0
    assert d.currency == "USD"
    assert d.probability == 60.0
    assert d.close_date == "2026-06-01"
    assert d.customer_external_id == "99"
    assert d.contact_external_id == "10"
    assert d.owner_name == "Rep A"
    assert d.metadata["pipeline_id"] == 1


@pytest.mark.asyncio
async def test_iter_deals_tolerates_missing_optional_fields(adapter):
    body = {
        "data": [{"id": 1, "title": "Minimal"}],
        "additional_data": {"pagination": {"more_items_in_collection": False}},
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    deals = await _collect(adapter.iter_deals())
    d = deals[0]
    assert d.title == "Minimal"
    assert d.amount is None
    assert d.probability is None


# ── Notes write-back ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_note_links_all_provided_targets(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"data": {"id": 777}, "success": True})
    )
    note_id = await adapter.create_note(
        content="Summary of the call",
        deal_external_id="55",
        contact_external_id="10",
        customer_external_id="99",
    )
    assert note_id == "777"
    call = adapter._client.request.await_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/v1/notes")
    body = call.kwargs["json"]
    assert body["content"] == "Summary of the call"
    assert body["deal_id"] == 55
    assert body["person_id"] == 10
    assert body["org_id"] == 99


@pytest.mark.asyncio
async def test_create_note_requires_a_target(adapter):
    with pytest.raises(CrmError):
        await adapter.create_note(content="Orphan note")


@pytest.mark.asyncio
async def test_create_note_rejects_empty_content(adapter):
    with pytest.raises(CrmError):
        await adapter.create_note(content="", deal_external_id="55")


@pytest.mark.asyncio
async def test_create_note_raises_on_missing_id_in_response(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"data": {}, "success": True})
    )
    with pytest.raises(CrmError):
        await adapter.create_note(content="hi", deal_external_id="1")


# ── Activities write-back ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_activity_uses_task_type_by_default(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(
            200, {"data": {"id": 123}, "success": True}
        )
    )
    act_id = await adapter.create_activity(
        subject="Follow up on pricing",
        activity_type="task",
        due_date="2026-05-01",
        note="Customer asked for volume discount",
        deal_external_id="55",
    )
    assert act_id == "123"
    body = adapter._client.request.await_args.kwargs["json"]
    assert body["subject"] == "Follow up on pricing"
    assert body["type"] == "task"
    assert body["due_date"] == "2026-05-01"
    assert body["deal_id"] == 55
    assert body["note"] == "Customer asked for volume discount"


# ── Deal stage + custom fields ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_deal_stage_puts_stage_id(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"success": True})
    )
    await adapter.update_deal_stage(deal_external_id="55", stage_external_id="4")
    call = adapter._client.request.await_args
    assert call.args[0] == "PUT"
    assert call.args[1].endswith("/v1/deals/55")
    assert call.kwargs["json"] == {"stage_id": 4}


@pytest.mark.asyncio
async def test_update_custom_fields_resolves_field_map(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(200, {"success": True})
    )
    await adapter.update_deal_custom_fields(
        deal_external_id="55",
        fields={"call_outcome": "demoed", "lead_temp": "hot", "unknown_key": "x"},
    )
    body = adapter._client.request.await_args.kwargs["json"]
    # Mapped keys should resolve to their Pipedrive hashes; unknown keys
    # get dropped so we don't send garbage.
    assert body == {"cf_outcome_hash": "demoed", "cf_temp_hash": "hot"}


@pytest.mark.asyncio
async def test_update_custom_fields_noop_on_all_unknown(adapter):
    adapter._client.request = AsyncMock()
    await adapter.update_deal_custom_fields(
        deal_external_id="55", fields={"unknown_only": "x"}
    )
    # No call made when nothing resolves.
    assert adapter._client.request.await_count == 0


# ── Base-protocol default behaviour ────────────────────────────────────


class _MinimalAdapter:
    """Adapter that only implements the read surface — uses the Protocol
    defaults for the write methods. Verifies the CrmCapabilityMissing
    contract callers rely on."""

    provider = "minimal"

    async def iter_customers(self):  # pragma: no cover — not invoked
        return
        yield

    async def iter_contacts(self):  # pragma: no cover
        return
        yield

    async def close(self):  # pragma: no cover
        return


@pytest.mark.asyncio
async def test_adapter_write_defaults_raise_capability_missing():
    """An adapter that doesn't implement the optional write methods
    should raise CrmCapabilityMissing rather than silently succeed."""
    from backend.app.services.crm.base import CrmAdapter

    # Directly invoke the protocol default stubs. Each optional method
    # is defined on the Protocol; using CrmAdapter as a mixin base
    # exposes them to non-overriding subclasses.
    class Impl(CrmAdapter, _MinimalAdapter):
        pass

    impl = Impl()
    with pytest.raises(CrmCapabilityMissing):
        await impl.create_note(content="x", deal_external_id="1")
    with pytest.raises(CrmCapabilityMissing):
        await impl.create_activity(subject="x", activity_type="task")
    with pytest.raises(CrmCapabilityMissing):
        await impl.update_deal_stage(deal_external_id="1", stage_external_id="2")
