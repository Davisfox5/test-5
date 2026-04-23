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
    CrmCapabilityMissing,  # re-exported for callers
    CrmContact,
    CrmCustomer,
    CrmDeal,
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
        field_map: Optional[Dict[str, str]] = None,
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
        # Custom-field mapping: LINDA field key → Pipedrive custom-field
        # hash (Pipedrive exposes custom fields as ``abcd1234…``-style
        # hashes on the deal/person/org row). Used on write-back so
        # insights land on the right columns in the tenant's pipeline.
        self._field_map: Dict[str, str] = dict(field_map or {})
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

    # ── Deals ─────────────────────────────────────────────────────

    async def iter_deals(self) -> AsyncIterator[CrmDeal]:
        async for row in self._paginate("/v1/deals"):
            status = str(row.get("status") or "").lower() or None
            yield CrmDeal(
                external_id=str(row.get("id", "")),
                title=str(row.get("title") or "Untitled deal"),
                stage=str(row.get("stage_id")) if row.get("stage_id") is not None else None,
                status=status if status in ("open", "won", "lost", "deleted") else status,
                amount=_float_or_none(row.get("value")),
                currency=row.get("currency"),
                probability=_float_or_none(row.get("probability")),
                close_date=row.get("close_time") or row.get("expected_close_date"),
                customer_external_id=_org_id(row.get("org_id")),
                contact_external_id=(
                    _org_id(row.get("person_id"))
                    if row.get("person_id") is not None
                    else None
                ),
                owner_name=(
                    (row.get("owner_id") or {}).get("name")
                    if isinstance(row.get("owner_id"), dict)
                    else None
                ),
                metadata={
                    "pipeline_id": row.get("pipeline_id"),
                    "won_time": row.get("won_time"),
                    "lost_time": row.get("lost_time"),
                    "lost_reason": row.get("lost_reason"),
                },
            )

    # ── Write-back ────────────────────────────────────────────────

    async def create_note(
        self,
        *,
        content: str,
        deal_external_id: Optional[str] = None,
        contact_external_id: Optional[str] = None,
        customer_external_id: Optional[str] = None,
    ) -> str:
        """Attach a note via ``POST /v1/notes``.

        At least one of deal/person/organization must be provided, per
        the Pipedrive API. We pass all supplied ids so the note is
        linked everywhere it makes sense.
        """
        if not content:
            raise CrmError("note content is required")
        if not any([deal_external_id, contact_external_id, customer_external_id]):
            raise CrmError(
                "Pipedrive notes require at least one target (deal/person/org)"
            )
        payload: Dict[str, Any] = {"content": content}
        if deal_external_id:
            payload["deal_id"] = int(deal_external_id)
        if contact_external_id:
            payload["person_id"] = int(contact_external_id)
        if customer_external_id:
            payload["org_id"] = int(customer_external_id)

        data = await self._post("/v1/notes", json=payload)
        note_id = ((data.get("data") or {}).get("id"))
        if note_id is None:
            raise CrmError("Pipedrive note response missing id")
        return str(note_id)

    async def create_activity(
        self,
        *,
        subject: str,
        activity_type: str,
        due_date: Optional[str] = None,
        note: Optional[str] = None,
        deal_external_id: Optional[str] = None,
        contact_external_id: Optional[str] = None,
    ) -> str:
        """Create a follow-up activity via ``POST /v1/activities``.

        ``activity_type`` must match an activity-type key that exists in
        the tenant's Pipedrive (``call``, ``meeting``, ``task``, or a
        custom one). The caller is responsible for picking a value the
        tenant actually has configured.
        """
        payload: Dict[str, Any] = {
            "subject": subject,
            "type": activity_type,
        }
        if due_date:
            payload["due_date"] = due_date
        if note:
            payload["note"] = note
        if deal_external_id:
            payload["deal_id"] = int(deal_external_id)
        if contact_external_id:
            payload["person_id"] = int(contact_external_id)

        data = await self._post("/v1/activities", json=payload)
        act_id = ((data.get("data") or {}).get("id"))
        if act_id is None:
            raise CrmError("Pipedrive activity response missing id")
        return str(act_id)

    async def update_deal_stage(
        self,
        *,
        deal_external_id: str,
        stage_external_id: str,
    ) -> None:
        """Move a deal to a different stage via ``PUT /v1/deals/{id}``."""
        await self._put(
            f"/v1/deals/{int(deal_external_id)}",
            json={"stage_id": int(stage_external_id)},
        )

    async def update_deal_custom_fields(
        self,
        *,
        deal_external_id: str,
        fields: Dict[str, Any],
    ) -> None:
        """Write a dict of LINDA-keyed fields into a Pipedrive deal.

        Each key is looked up in the adapter's ``field_map`` to resolve
        the Pipedrive custom-field hash. Unknown keys are silently
        dropped with a warning — better than failing the whole write
        because one mapping was missing.
        """
        if not fields:
            return
        mapped: Dict[str, Any] = {}
        for key, value in fields.items():
            pd_hash = self._field_map.get(key)
            if not pd_hash:
                logger.warning(
                    "Pipedrive field_map missing entry for '%s'; skipping", key
                )
                continue
            mapped[pd_hash] = value
        if not mapped:
            return
        await self._put(
            f"/v1/deals/{int(deal_external_id)}",
            json=mapped,
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
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        headers: Dict[str, str] = {"Accept": "application/json"}
        request_params = dict(params or {})
        if self._auth_mode == "api_token":
            request_params["api_token"] = self._access_token
        else:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        return await self._client.request(
            method,
            f"{self._api_domain}{path}",
            params=request_params,
            headers=headers,
            json=json_body,
        )

    async def _post(self, path: str, *, json: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request("POST", path, json_body=json)
        if resp.status_code == 401 and self._refresh_token and self._auth_mode == "bearer":
            await self._refresh_access_token()
            resp = await self._request("POST", path, json_body=json)
        if resp.status_code == 401:
            raise CrmAuthError("Pipedrive rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("Pipedrive rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"Pipedrive POST {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    async def _put(self, path: str, *, json: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request("PUT", path, json_body=json)
        if resp.status_code == 401 and self._refresh_token and self._auth_mode == "bearer":
            await self._refresh_access_token()
            resp = await self._request("PUT", path, json_body=json)
        if resp.status_code == 401:
            raise CrmAuthError("Pipedrive rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("Pipedrive rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"Pipedrive PUT {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.content else {}

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


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["PipedriveAdapter", "CrmCapabilityMissing"]
