"""Unit tests for the CRM adapters.

We mock ``httpx.AsyncClient.request`` on each adapter instance so the
tests are offline + deterministic. Focus: pagination, property mapping,
auth refresh fallback, rate-limit surfaces.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.crm.base import (
    CrmAuthError,
    CrmContact,
    CrmCustomer,
    CrmRateLimitError,
)
from backend.app.services.crm.hubspot import HubSpotAdapter
from backend.app.services.crm.salesforce import SalesforceAdapter


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


# ── HubSpot ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hubspot_iter_customers_paginates_and_maps():
    adapter = HubSpotAdapter(access_token="tok", refresh_token="ref")
    pages = [
        _mock_response(
            200,
            {
                "results": [
                    {
                        "id": "101",
                        "properties": {
                            "name": "Acme Corp",
                            "domain": "acme.example",
                            "industry": "Software",
                            "city": "NYC",
                        },
                    }
                ],
                "paging": {"next": {"after": "cursor-1"}},
            },
        ),
        _mock_response(
            200,
            {
                "results": [
                    {
                        "id": "102",
                        "properties": {
                            "name": "Globex",
                            "domain": "globex.example",
                        },
                    }
                ],
                # No paging.next → stop.
            },
        ),
    ]
    adapter._client.request = AsyncMock(side_effect=pages)

    customers = await _collect(adapter.iter_customers())
    assert len(customers) == 2
    assert isinstance(customers[0], CrmCustomer)
    assert customers[0].name == "Acme Corp"
    assert customers[0].industry == "Software"
    assert customers[0].metadata["city"] == "NYC"
    assert customers[1].external_id == "102"
    assert adapter._client.request.await_count == 2


@pytest.mark.asyncio
async def test_hubspot_iter_contacts_includes_company_association():
    adapter = HubSpotAdapter(access_token="tok")
    body = {
        "results": [
            {
                "id": "c1",
                "properties": {
                    "firstname": "Sarah",
                    "lastname": "Lee",
                    "email": "sarah@acme.com",
                    "phone": "+15551234",
                    "jobtitle": "CFO",
                },
                "associations": {
                    "companies": {"results": [{"id": "101", "type": "primary"}]}
                },
            }
        ]
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    contacts = await _collect(adapter.iter_contacts())
    assert len(contacts) == 1
    assert isinstance(contacts[0], CrmContact)
    assert contacts[0].name == "Sarah Lee"
    assert contacts[0].email == "sarah@acme.com"
    assert contacts[0].customer_external_id == "101"
    assert contacts[0].metadata["job_title"] == "CFO"


@pytest.mark.asyncio
async def test_hubspot_401_with_no_refresh_token_raises_auth_error():
    adapter = HubSpotAdapter(access_token="tok")  # no refresh token
    adapter._client.request = AsyncMock(return_value=_mock_response(401, {}))
    with pytest.raises(CrmAuthError):
        await _collect(adapter.iter_customers())


@pytest.mark.asyncio
async def test_hubspot_rate_limit_raises():
    adapter = HubSpotAdapter(access_token="tok")
    adapter._client.request = AsyncMock(return_value=_mock_response(429, {}))
    with pytest.raises(CrmRateLimitError):
        await _collect(adapter.iter_customers())


# ── Salesforce ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_salesforce_iter_customers_paginates_via_next_records_url():
    adapter = SalesforceAdapter(
        access_token="tok",
        instance_url="https://na1.salesforce.com",
    )
    pages = [
        _mock_response(
            200,
            {
                "records": [
                    {
                        "Id": "001A",
                        "Name": "Acme",
                        "Website": "https://acme.example/",
                        "Industry": "Software",
                    }
                ],
                "done": False,
                "nextRecordsUrl": "/services/data/v59.0/query/01g-next",
            },
        ),
        _mock_response(
            200,
            {
                "records": [{"Id": "001B", "Name": "Globex"}],
                "done": True,
            },
        ),
    ]
    adapter._client.request = AsyncMock(side_effect=pages)
    customers = await _collect(adapter.iter_customers())
    assert [c.external_id for c in customers] == ["001A", "001B"]
    # Website maps to domain, https:// stripped.
    assert customers[0].domain == "acme.example"


@pytest.mark.asyncio
async def test_salesforce_contacts_use_account_id():
    adapter = SalesforceAdapter(access_token="tok", instance_url="https://x.sf.com")
    body = {
        "records": [
            {
                "Id": "C1",
                "FirstName": "Sarah",
                "LastName": "Lee",
                "Email": "sarah@acme.com",
                "AccountId": "001A",
                "Title": "CFO",
            }
        ],
        "done": True,
    }
    adapter._client.request = AsyncMock(return_value=_mock_response(200, body))
    contacts = await _collect(adapter.iter_contacts())
    assert contacts[0].customer_external_id == "001A"
    assert contacts[0].metadata["job_title"] == "CFO"


@pytest.mark.asyncio
async def test_salesforce_requires_instance_url():
    with pytest.raises(CrmAuthError):
        SalesforceAdapter(access_token="tok", instance_url="")
