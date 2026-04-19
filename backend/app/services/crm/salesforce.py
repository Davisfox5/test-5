"""Salesforce CRM adapter.

Salesforce stores the org's instance URL alongside the token — we keep it
in ``Integration.provider_config["instance_url"]``. Queries use the REST
SOQL endpoint and paginate via ``nextRecordsUrl``.

Token refresh uses the OAuth 2.0 refresh_token grant at
``{instance_url}/services/oauth2/token``.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from backend.app.config import get_settings
from backend.app.services.crm.base import (
    CrmAuthError,
    CrmContact,
    CrmCustomer,
    CrmError,
    CrmRateLimitError,
)

logger = logging.getLogger(__name__)

_API_VERSION = "v59.0"


class SalesforceAdapter:
    provider = "salesforce"

    def __init__(
        self,
        access_token: str,
        instance_url: str,
        refresh_token: Optional[str] = None,
        on_token_refresh=None,
    ) -> None:
        if not access_token:
            raise CrmAuthError("Salesforce access_token is required")
        if not instance_url:
            raise CrmAuthError("Salesforce instance_url is required")
        self._access_token = access_token
        self._instance_url = instance_url.rstrip("/")
        self._refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def iter_customers(self) -> AsyncIterator[CrmCustomer]:
        query = (
            "SELECT Id, Name, Website, Industry, BillingCity, BillingCountry, "
            "NumberOfEmployees FROM Account"
        )
        async for row in self._paginate_soql(query):
            yield CrmCustomer(
                external_id=str(row.get("Id", "")),
                name=str(row.get("Name") or "Untitled"),
                domain=(row.get("Website") or "").replace("https://", "").replace("http://", "").strip("/") or None,
                industry=row.get("Industry"),
                metadata={
                    "city": row.get("BillingCity"),
                    "country": row.get("BillingCountry"),
                    "employees": row.get("NumberOfEmployees"),
                },
            )

    async def iter_contacts(self) -> AsyncIterator[CrmContact]:
        query = (
            "SELECT Id, FirstName, LastName, Email, Phone, Title, AccountId "
            "FROM Contact"
        )
        async for row in self._paginate_soql(query):
            first = row.get("FirstName") or ""
            last = row.get("LastName") or ""
            name = (f"{first} {last}").strip() or row.get("Email") or None
            yield CrmContact(
                external_id=str(row.get("Id", "")),
                name=name,
                email=row.get("Email"),
                phone=row.get("Phone"),
                customer_external_id=row.get("AccountId"),
                metadata={"job_title": row.get("Title")},
            )

    async def _paginate_soql(self, query: str):
        next_path = f"/services/data/{_API_VERSION}/query?q={self._encode_query(query)}"
        while next_path:
            data = await self._get(next_path)
            for row in data.get("records", []):
                yield row
            if data.get("done"):
                break
            next_path = data.get("nextRecordsUrl")

    @staticmethod
    def _encode_query(q: str) -> str:
        # Salesforce accepts URL-encoded SOQL; keep it simple.
        from urllib.parse import quote
        return quote(q, safe="")

    async def _get(self, path: str) -> Dict[str, Any]:
        resp = await self._request("GET", path)
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._request("GET", path)
        if resp.status_code == 401:
            raise CrmAuthError("Salesforce rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("Salesforce rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"Salesforce {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    async def _request(self, method: str, path: str) -> httpx.Response:
        return await self._client.request(
            method,
            f"{self._instance_url}{path}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
        )

    async def _refresh_access_token(self) -> None:
        settings = get_settings()
        client_id = settings.SALESFORCE_CLIENT_ID
        client_secret = settings.SALESFORCE_CLIENT_SECRET
        if not (client_id and client_secret and self._refresh_token):
            raise CrmAuthError("Salesforce refresh credentials missing")
        resp = await self._client.post(
            f"{self._instance_url}/services/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": self._refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise CrmAuthError(
                f"Salesforce token refresh failed: {resp.status_code}"
            )
        body = resp.json()
        self._access_token = body.get("access_token") or self._access_token
        new_instance = body.get("instance_url")
        if new_instance:
            self._instance_url = new_instance.rstrip("/")
        if self._on_token_refresh is not None:
            try:
                await self._on_token_refresh(
                    self._access_token,
                    self._refresh_token,
                    None,  # Salesforce doesn't return expires_in here
                    {"instance_url": self._instance_url},
                )
            except Exception:
                logger.exception("on_token_refresh callback failed")
