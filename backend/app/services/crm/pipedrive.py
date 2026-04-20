"""Pipedrive CRM adapter.

Pipedrive uses a tenant-specific ``api_domain`` (e.g.
``https://foo.pipedrive.com``) stored in
``Integration.provider_config["api_domain"]``. Auth is Bearer for OAuth
installs and falls back to ``?api_token=`` for legacy PAT installs
(provider_config["auth_mode"] = "api_token").

Pagination contract (v1): responses carry ``additional_data.pagination``
with ``more_items_in_collection`` and ``next_start``. We loop until that
goes false.

Token refresh: Pipedrive exposes the standard OAuth refresh_token grant
at ``https://oauth.pipedrive.com/oauth/token``. A new refresh_token may
be returned; we surface it via the on_token_refresh callback.
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

_OAUTH_TOKEN_URL = "https://oauth.pipedrive.com/oauth/token"


class PipedriveAdapter:
    provider = "pipedrive"

    def __init__(
        self,
        access_token: str,
        api_domain: str,
        refresh_token: Optional[str] = None,
        auth_mode: str = "bearer",  # "bearer" (OAuth) | "api_token" (PAT)
        on_token_refresh=None,
    ) -> None:
        if not access_token:
            raise CrmAuthError("Pipedrive access_token is required")
        if not api_domain:
            raise CrmAuthError(
                "Pipedrive api_domain is required (stored in provider_config)"
            )
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._api_domain = api_domain.rstrip("/")
        self._auth_mode = auth_mode
        self._on_token_refresh = on_token_refresh
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def iter_customers(self) -> AsyncIterator[CrmCustomer]:
        async for row in self._paginate("/v1/organizations"):
            yield CrmCustomer(
                external_id=str(row.get("id", "")),
                name=str(row.get("name") or "Untitled"),
                # Pipedrive doesn't expose a normalized domain on orgs; pull
                # from custom fields if present.
                domain=row.get("web_domain") or None,
                industry=row.get("industry"),
                metadata={
                    "country": row.get("address_country"),
                    "owner_name": (row.get("owner_id") or {}).get("name")
                    if isinstance(row.get("owner_id"), dict)
                    else None,
                    "people_count": row.get("people_count"),
                },
            )

    async def iter_contacts(self) -> AsyncIterator[CrmContact]:
        async for row in self._paginate("/v1/persons"):
            yield CrmContact(
                external_id=str(row.get("id", "")),
                name=row.get("name"),
                email=_first_label_value(row.get("email")),
                phone=_first_label_value(row.get("phone")),
                customer_external_id=_org_id(row.get("org_id")),
                metadata={
                    "job_title": row.get("job_title"),
                },
            )

    async def _paginate(self, path: str) -> AsyncIterator[Dict[str, Any]]:
        start = 0
        while True:
            params: Dict[str, Any] = {"limit": 100, "start": start}
            data = await self._get(path, params)
            for row in data.get("data") or []:
                yield row
            pagination = (data.get("additional_data") or {}).get("pagination") or {}
            if not pagination.get("more_items_in_collection"):
                break
            next_start = pagination.get("next_start")
            if not isinstance(next_start, int) or next_start <= start:
                # Defensive: avoid infinite loops on a misbehaving response.
                break
            start = next_start

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request("GET", path, params)
        if resp.status_code == 401 and self._refresh_token and self._auth_mode == "bearer":
            await self._refresh_access_token()
            resp = await self._request("GET", path, params)
        if resp.status_code == 401:
            raise CrmAuthError("Pipedrive rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("Pipedrive rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"Pipedrive {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    async def _request(
        self, method: str, path: str, params: Dict[str, Any]
    ) -> httpx.Response:
        headers: Dict[str, str] = {"Accept": "application/json"}
        request_params = dict(params)
        if self._auth_mode == "api_token":
            request_params["api_token"] = self._access_token
        else:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return await self._client.request(
            method,
            f"{self._api_domain}{path}",
            params=request_params,
            headers=headers,
        )

    async def _refresh_access_token(self) -> None:
        settings = get_settings()
        client_id = settings.PIPEDRIVE_CLIENT_ID
        client_secret = settings.PIPEDRIVE_CLIENT_SECRET
        if not (client_id and client_secret and self._refresh_token):
            raise CrmAuthError("Pipedrive refresh credentials missing")
        resp = await self._client.post(
            _OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise CrmAuthError(
                f"Pipedrive token refresh failed: {resp.status_code}"
            )
        body = resp.json()
        self._access_token = body.get("access_token") or self._access_token
        new_refresh = body.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
        new_domain = body.get("api_domain")
        if new_domain:
            self._api_domain = new_domain.rstrip("/")
        if self._on_token_refresh is not None:
            try:
                await self._on_token_refresh(
                    self._access_token,
                    self._refresh_token,
                    body.get("expires_in"),
                    {"api_domain": self._api_domain} if new_domain else None,
                )
            except Exception:
                logger.exception("on_token_refresh callback failed")


# ── Helpers ───────────────────────────────────────────────────────────


def _first_label_value(values: Any) -> Optional[str]:
    """Pipedrive returns email/phone as ``[{label, value, primary}]``.
    Return the primary one (or the first) as a plain string."""
    if not isinstance(values, list):
        return None
    if not values:
        return None
    for entry in values:
        if isinstance(entry, dict) and entry.get("primary") and entry.get("value"):
            return str(entry["value"])
    for entry in values:
        if isinstance(entry, dict) and entry.get("value"):
            return str(entry["value"])
    return None


def _org_id(org: Any) -> Optional[str]:
    """Pipedrive expands org_id as a dict on some endpoints, a bare int on
    others. Handle both."""
    if org is None:
        return None
    if isinstance(org, dict):
        val = org.get("value") or org.get("id")
        return str(val) if val is not None else None
    return str(org)
