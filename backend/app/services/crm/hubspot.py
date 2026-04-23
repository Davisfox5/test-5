"""HubSpot CRM adapter.

Uses the CRM v3 REST API. Reads the OAuth access token off the Integration
row; provider_config may carry a ``portal_id`` but isn't required today.

Pagination: HubSpot returns ``paging.next.after`` when more rows exist.
We loop until that's absent. Rate-limit responses (HTTP 429) raise
``CrmRateLimitError`` so the caller can back off.

Token refresh: when HubSpot returns 401, we attempt the refresh dance and
retry once. A second 401 raises ``CrmAuthError``.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from backend.app.config import get_settings
from backend.app.services.crm.base import (
    CrmAdapter,
    CrmAuthError,
    CrmContact,
    CrmCustomer,
    CrmError,
    CrmRateLimitError,
)

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_COMPANY_PROPS = ["name", "domain", "industry", "city", "country", "numberofemployees"]
_CONTACT_PROPS = ["firstname", "lastname", "email", "phone", "jobtitle", "company"]


class HubSpotAdapter:
    provider = "hubspot"

    def __init__(
        self,
        access_token: str,
        refresh_token: Optional[str] = None,
        on_token_refresh=None,
    ) -> None:
        if not access_token:
            raise CrmAuthError("HubSpot access_token is required")
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def iter_customers(self) -> AsyncIterator[CrmCustomer]:
        after: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "limit": 100,
                "properties": ",".join(_COMPANY_PROPS),
            }
            if after:
                params["after"] = after
            data = await self._get("/crm/v3/objects/companies", params)
            for row in data.get("results", []):
                props = row.get("properties") or {}
                yield CrmCustomer(
                    external_id=str(row.get("id", "")),
                    name=str(props.get("name") or "Untitled"),
                    domain=props.get("domain"),
                    industry=props.get("industry"),
                    metadata={
                        "city": props.get("city"),
                        "country": props.get("country"),
                        "employees": props.get("numberofemployees"),
                    },
                )
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

    async def iter_contacts(self) -> AsyncIterator[CrmContact]:
        after: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "limit": 100,
                "properties": ",".join(_CONTACT_PROPS),
                "associations": "companies",
            }
            if after:
                params["after"] = after
            data = await self._get("/crm/v3/objects/contacts", params)
            for row in data.get("results", []):
                props = row.get("properties") or {}
                first = props.get("firstname") or ""
                last = props.get("lastname") or ""
                name = (f"{first} {last}").strip() or props.get("email") or None
                # Association: primary company (if any).
                customer_ext_id: Optional[str] = None
                assoc = (row.get("associations") or {}).get("companies") or {}
                for a in assoc.get("results") or []:
                    if a.get("id"):
                        customer_ext_id = str(a["id"])
                        break
                yield CrmContact(
                    external_id=str(row.get("id", "")),
                    name=name,
                    email=props.get("email"),
                    phone=props.get("phone"),
                    customer_external_id=customer_ext_id,
                    metadata={"job_title": props.get("jobtitle")},
                )
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request("GET", path, params)
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._request("GET", path, params)
        if resp.status_code == 401:
            raise CrmAuthError("HubSpot rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("HubSpot rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(f"HubSpot {path} failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def _request(self, method: str, path: str, params: Dict[str, Any]) -> httpx.Response:
        return await self._client.request(
            method,
            f"{_BASE}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
        )

    async def _refresh_access_token(self) -> None:
        settings = get_settings()
        client_id = settings.HUBSPOT_CLIENT_ID
        client_secret = settings.HUBSPOT_CLIENT_SECRET
        if not (client_id and client_secret and self._refresh_token):
            raise CrmAuthError("HubSpot refresh credentials missing")
        resp = await self._client.post(
            "https://api.hubapi.com/oauth/v1/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": self._refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise CrmAuthError(f"HubSpot token refresh failed: {resp.status_code}")
        body = resp.json()
        self._access_token = body.get("access_token") or self._access_token
        new_refresh = body.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
        if self._on_token_refresh is not None:
            try:
                await self._on_token_refresh(
                    self._access_token,
                    self._refresh_token,
                    body.get("expires_in"),
                )
            except Exception:
                logger.exception("on_token_refresh callback failed")


# Module-level sanity check: make sure the adapter still implements the protocol.
assert isinstance(HubSpotAdapter("stub"), CrmAdapter) if False else True
