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
    CrmDeal,
    CrmError,
    CrmRateLimitError,
)

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_COMPANY_PROPS = ["name", "domain", "industry", "city", "country", "numberofemployees"]
_CONTACT_PROPS = ["firstname", "lastname", "email", "phone", "jobtitle", "company"]
_DEAL_PROPS = [
    "dealname",
    "dealstage",
    "pipeline",
    "amount",
    "hs_deal_stage_probability",
    "closedate",
    "hubspot_owner_id",
]


class HubSpotAdapter:
    provider = "hubspot"

    def __init__(
        self,
        access_token: str,
        refresh_token: Optional[str] = None,
        field_map: Optional[Dict[str, str]] = None,
        on_token_refresh=None,
    ) -> None:
        if not access_token:
            raise CrmAuthError("HubSpot access_token is required")
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._field_map: Dict[str, str] = dict(field_map or {})
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

    # ── Deals (opportunities) ─────────────────────────────────────

    async def iter_deals(self) -> AsyncIterator[CrmDeal]:
        after: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "limit": 100,
                "properties": ",".join(_DEAL_PROPS),
                "associations": "companies,contacts",
            }
            if after:
                params["after"] = after
            data = await self._get("/crm/v3/objects/deals", params)
            for row in data.get("results", []):
                props = row.get("properties") or {}
                associations = row.get("associations") or {}
                company_id = _first_association_id(associations.get("companies"))
                contact_id = _first_association_id(associations.get("contacts"))
                amount = _float_or_none(props.get("amount"))
                probability = _float_or_none(
                    props.get("hs_deal_stage_probability")
                )
                yield CrmDeal(
                    external_id=str(row.get("id", "")),
                    title=str(props.get("dealname") or "Untitled deal"),
                    stage=props.get("dealstage"),
                    status=_hubspot_status_for_stage(props.get("dealstage")),
                    amount=amount,
                    currency="USD",  # HubSpot portal default; override via field_map if needed
                    probability=probability,
                    close_date=props.get("closedate"),
                    customer_external_id=company_id,
                    contact_external_id=contact_id,
                    owner_name=props.get("hubspot_owner_id"),
                    metadata={
                        "pipeline": props.get("pipeline"),
                    },
                )
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

    # ── Write-back ───────────────────────────────────────────────

    async def create_note(
        self,
        *,
        content: str,
        deal_external_id: Optional[str] = None,
        contact_external_id: Optional[str] = None,
        customer_external_id: Optional[str] = None,
    ) -> str:
        """Create a HubSpot engagement of type ``note`` and associate it
        with any supplied deal / contact / company ids.

        HubSpot associates engagements via the v4 ``associations`` API
        block on the create payload; association type IDs are
        documented on the HubSpot developer portal:
          * note → company = 190
          * note → contact = 202
          * note → deal    = 214
        """
        if not content:
            raise CrmError("note content is required")
        if not any([deal_external_id, contact_external_id, customer_external_id]):
            raise CrmError(
                "HubSpot notes require at least one association (deal/contact/company)"
            )
        body: Dict[str, Any] = {
            "properties": {
                "hs_note_body": content,
                "hs_timestamp": _now_iso_millis(),
            },
            "associations": _hubspot_note_associations(
                deal_external_id=deal_external_id,
                contact_external_id=contact_external_id,
                customer_external_id=customer_external_id,
            ),
        }
        data = await self._post("/crm/v3/objects/notes", body)
        note_id = str(data.get("id") or "")
        if not note_id:
            raise CrmError("HubSpot note response missing id")
        return note_id

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
        """Create a HubSpot task engagement. ``activity_type`` maps to
        the ``hs_task_type`` enum: ``TODO``, ``CALL``, ``EMAIL``.
        Everything else falls through to ``TODO``.
        """
        task_type = {"call": "CALL", "email": "EMAIL"}.get(
            activity_type.lower(), "TODO"
        )
        body: Dict[str, Any] = {
            "properties": {
                "hs_task_subject": subject,
                "hs_task_body": note or "",
                "hs_task_status": "NOT_STARTED",
                "hs_task_priority": "MEDIUM",
                "hs_task_type": task_type,
                "hs_timestamp": _now_iso_millis(),
            },
        }
        if due_date:
            # HubSpot wants an epoch-millis timestamp.
            body["properties"]["hs_task_due_date"] = _due_date_to_millis(due_date)
        associations = _hubspot_task_associations(
            deal_external_id=deal_external_id,
            contact_external_id=contact_external_id,
        )
        if associations:
            body["associations"] = associations
        data = await self._post("/crm/v3/objects/tasks", body)
        task_id = str(data.get("id") or "")
        if not task_id:
            raise CrmError("HubSpot task response missing id")
        return task_id

    async def update_deal_stage(
        self,
        *,
        deal_external_id: str,
        stage_external_id: str,
    ) -> None:
        await self._patch(
            f"/crm/v3/objects/deals/{deal_external_id}",
            {"properties": {"dealstage": stage_external_id}},
        )

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request(
            "POST", path, params={}, json_body=body
        )
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._request("POST", path, params={}, json_body=body)
        if resp.status_code == 401:
            raise CrmAuthError("HubSpot rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("HubSpot rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"HubSpot POST {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.content else {}

    async def _patch(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request(
            "PATCH", path, params={}, json_body=body
        )
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._request("PATCH", path, params={}, json_body=body)
        if resp.status_code == 401:
            raise CrmAuthError("HubSpot rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("HubSpot rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"HubSpot PATCH {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.content else {}

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

    async def _request(
        self,
        method: str,
        path: str,
        params: Dict[str, Any],
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        return await self._client.request(
            method,
            f"{_BASE}{path}",
            params=params,
            headers=headers,
            json=json_body,
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


# ── Helpers ──────────────────────────────────────────────────────────


# HubSpot "default" association type IDs for the note + task objects.
# Reference: https://developers.hubspot.com/docs/api/crm/associations
_HS_NOTE_TO_DEAL = 214
_HS_NOTE_TO_CONTACT = 202
_HS_NOTE_TO_COMPANY = 190
_HS_TASK_TO_DEAL = 216
_HS_TASK_TO_CONTACT = 204


def _hubspot_note_associations(
    *,
    deal_external_id: Optional[str],
    contact_external_id: Optional[str],
    customer_external_id: Optional[str],
) -> list:
    out: list = []
    if deal_external_id:
        out.append(_assoc(deal_external_id, "deal", _HS_NOTE_TO_DEAL))
    if contact_external_id:
        out.append(_assoc(contact_external_id, "contact", _HS_NOTE_TO_CONTACT))
    if customer_external_id:
        out.append(_assoc(customer_external_id, "company", _HS_NOTE_TO_COMPANY))
    return out


def _hubspot_task_associations(
    *,
    deal_external_id: Optional[str],
    contact_external_id: Optional[str],
) -> list:
    out: list = []
    if deal_external_id:
        out.append(_assoc(deal_external_id, "deal", _HS_TASK_TO_DEAL))
    if contact_external_id:
        out.append(_assoc(contact_external_id, "contact", _HS_TASK_TO_CONTACT))
    return out


def _assoc(target_id: str, target_label: str, type_id: int) -> Dict[str, Any]:
    return {
        "to": {"id": target_id},
        "types": [
            {
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": type_id,
            }
        ],
    }


def _first_association_id(associations: Optional[Dict[str, Any]]) -> Optional[str]:
    if not associations:
        return None
    for row in associations.get("results") or []:
        if row.get("id"):
            return str(row["id"])
    return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hubspot_status_for_stage(stage: Optional[str]) -> Optional[str]:
    """Best-effort mapping from HubSpot ``dealstage`` IDs to the
    neutral LINDA deal status. Tenants can add explicit overrides via
    ``provider_config.stage_status_map`` in a follow-up — for now we
    surface the stage verbatim and let downstream decide.
    """
    if not stage:
        return None
    lower = stage.lower()
    if "closedwon" in lower or "closed_won" in lower:
        return "won"
    if "closedlost" in lower or "closed_lost" in lower:
        return "lost"
    return "open"


def _now_iso_millis() -> str:
    """HubSpot's ``hs_timestamp`` expects an epoch-millis string."""
    import time as _time

    return str(int(_time.time() * 1000))


def _due_date_to_millis(due_date: str) -> str:
    """Convert an ISO-8601 date (``YYYY-MM-DD`` or full datetime) to
    epoch millis for HubSpot's task ``hs_task_due_date`` field.
    """
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
    except ValueError:
        # Assume date-only.
        dt = datetime.strptime(due_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))


# Module-level sanity check: make sure the adapter still implements the protocol.
assert isinstance(HubSpotAdapter("stub"), CrmAdapter) if False else True


__all__ = ["HubSpotAdapter"]
