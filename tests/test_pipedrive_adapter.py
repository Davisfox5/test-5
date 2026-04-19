"""Pipedrive adapter tests — pagination, email/phone unwrapping, auth modes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import (
    CrmAuthError,
    CrmRateLimitError,
)
from backend.app.services.crm.pipedrive import (
    PipedriveAdapter,
    _first_label_value,
    _org_id,
)


def _mock_response(status: int, body: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
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
    )


# ── Pure helpers ────────────────────────────────────────────────────


def test_first_label_value_prefers_primary():
    values = [
        {"label": "work", "value": "a@x.com", "primary": False},
        {"label": "home", "value": "b@x.com", "primary": True},
    ]
    assert _first_label_value(values) == "b@x.com"


def test_first_label_value_falls_back_to_first():
    values = [
        {"label": "work", "value": "a@x.com"},
        {"label": "home", "value": "b@x.com"},
    ]
    assert _first_label_value(values) == "a@x.com"


def test_first_label_value_handles_missing_and_non_list():
    assert _first_label_value(None) is None
    assert _first_label_value([]) is None
    assert _first_label_value("not a list") is None


def test_org_id_accepts_dict_and_int():
    assert _org_id({"value": 42, "name": "Acme"}) == "42"
    assert _org_id(42) == "42"
    assert _org_id(None) is None


# ── Pagination ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_iter_customers_paginates_until_more_items_false(adapter):
    pages = [
        _mock_response(
            200,
            {
                "data": [
                    {"id": 1, "name": "Acme", "industry": "Software"},
                ],
                "additional_data": {
                    "pagination": {
                        "more_items_in_collection": True,
                        "next_start": 100,
                    }
                },
            },
        ),
        _mock_response(
            200,
            {
                "data": [{"id": 2, "name": "Globex"}],
                "additional_data": {
                    "pagination": {"more_items_in_collection": False}
                },
            },
        ),
    ]
    adapter._client.request = AsyncMock(side_effect=pages)
    customers = await _collect(adapter.iter_customers())
    assert [c.name for c in customers] == ["Acme", "Globex"]
    # Second call used next_start=100.
    second_call_kwargs = adapter._client.request.await_args_list[1].kwargs
    assert second_call_kwargs["params"]["start"] == 100


@pytest.mark.asyncio
async def test_iter_customers_breaks_on_missing_next_start(adapter):
    """Defensive path: provider claims more items but omits/repeats
    next_start — we bail rather than loop forever."""
    adapter._client.request = AsyncMock(
        return_value=_mock_response(
            200,
            {
                "data": [{"id": 1, "name": "Acme"}],
                "additional_data": {
                    "pagination": {
                        "more_items_in_collection": True,
                        "next_start": 0,  # not greater than current start (0)
                    }
                },
            },
        )
    )
    customers = await _collect(adapter.iter_customers())
    assert len(customers) == 1
    assert adapter._client.request.await_count == 1


# ── Contacts mapping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_iter_contacts_unwraps_email_phone_and_org(adapter):
    body = {
        "data": [
            {
                "id": 7,
                "name": "Sarah Lee",
                "email": [
                    {"label": "work", "value": "sarah@acme.com", "primary": True},
                    {"label": "home", "value": "sarah@home.com"},
                ],
                "phone": [
                    {"label": "work", "value": "+15551234", "primary": False},
                ],
                "org_id": {"value": 101, "name": "Acme"},
                "job_title": "CFO",
            }
        ],
        "additional_data": {"pagination": {"more_items_in_collection": False}},
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    contacts = await _collect(adapter.iter_contacts())
    assert len(contacts) == 1
    c = contacts[0]
    assert c.email == "sarah@acme.com"
    assert c.phone == "+15551234"
    assert c.customer_external_id == "101"
    assert c.metadata["job_title"] == "CFO"


# ── Auth behaviour ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_token_mode_puts_token_in_query(adapter):
    adapter._auth_mode = "api_token"
    adapter._client.request = AsyncMock(
        return_value=_mock_response(
            200,
            {
                "data": [],
                "additional_data": {
                    "pagination": {"more_items_in_collection": False}
                },
            },
        )
    )
    await _collect(adapter.iter_customers())
    call = adapter._client.request.await_args
    assert call.kwargs["params"].get("api_token") == "tok"
    assert "Authorization" not in call.kwargs["headers"]


@pytest.mark.asyncio
async def test_401_without_refresh_token_raises_auth_error():
    adapter = PipedriveAdapter(
        access_token="tok",
        api_domain="https://foo.pipedrive.com",
        refresh_token=None,
    )
    adapter._client.request = AsyncMock(
        return_value=_mock_response(401, {"error": "invalid_token"})
    )
    with pytest.raises(CrmAuthError):
        await _collect(adapter.iter_customers())


@pytest.mark.asyncio
async def test_429_raises_rate_limit(adapter):
    adapter._client.request = AsyncMock(
        return_value=_mock_response(429, {"error": "rate limit"})
    )
    with pytest.raises(CrmRateLimitError):
        await _collect(adapter.iter_customers())


def test_requires_api_domain():
    with pytest.raises(CrmAuthError):
        PipedriveAdapter(access_token="tok", api_domain="")
