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
    CrmDeal,
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
        field_map: Optional[Dict[str, str]] = None,
        on_token_refresh=None,
    ) -> None:
        if not access_token:
            raise CrmAuthError("Salesforce access_token is required")
        if not instance_url:
            raise CrmAuthError("Salesforce instance_url is required")
        self._access_token = access_token
        self._instance_url = instance_url.rstrip("/")
        self._refresh_token = refresh_token
        self._field_map: Dict[str, str] = dict(field_map or {})
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

    # ── Deals (opportunities) ─────────────────────────────────────

    async def iter_deals(self) -> AsyncIterator[CrmDeal]:
        query = (
            "SELECT Id, Name, StageName, Amount, Probability, CloseDate, "
            "IsClosed, IsWon, AccountId, OwnerId FROM Opportunity"
        )
        async for row in self._paginate_soql(query):
            status = (
                "won" if row.get("IsWon") else
                "lost" if row.get("IsClosed") else "open"
            )
            yield CrmDeal(
                external_id=str(row.get("Id", "")),
                title=str(row.get("Name") or "Untitled deal"),
                stage=row.get("StageName"),
                status=status,
                amount=_float_or_none(row.get("Amount")),
                currency="USD",  # per-tenant override goes in provider_config
                probability=_float_or_none(row.get("Probability")),
                close_date=row.get("CloseDate"),
                customer_external_id=row.get("AccountId"),
                contact_external_id=None,  # Opportunity↔Contact via junction object
                owner_name=row.get("OwnerId"),
                metadata={"is_closed": row.get("IsClosed")},
            )

    # ── Write-back ───────────────────────────────────────────────

    async def create_note(
        self,
        *,
        content: str,
        deal_external_id: Optional[str] = None,
        contact_external_id: Optional[str] = None,
        customer_external_id: Optional[str] = None,
    ) -> str:
        """Create a Salesforce ``Note`` record.

        Salesforce deprecated the classic Note sObject in favor of
        ``ContentNote`` + ``ContentDocumentLink``; we use ``ContentNote``
        and link it to the first provided parent id (deal → Opportunity,
        contact → Contact, customer → Account).
        """
        if not content:
            raise CrmError("note content is required")
        parent_id = deal_external_id or contact_external_id or customer_external_id
        if not parent_id:
            raise CrmError(
                "Salesforce notes require at least one parent (deal/contact/customer)"
            )

        note_payload = {
            "Title": _truncate(content.split("\n", 1)[0], 80),
            "Content": _base64_utf8(content),
        }
        note_resp = await self._post(
            f"/services/data/{_API_VERSION}/sobjects/ContentNote", note_payload
        )
        note_id = note_resp.get("id")
        if not note_id:
            raise CrmError("Salesforce note response missing id")

        # Link the note to its parent. ShareType=V gives viewer access
        # to everyone who can see the parent — the usual CRM default.
        link_payload = {
            "ContentDocumentId": note_id,
            "LinkedEntityId": parent_id,
            "ShareType": "V",
            "Visibility": "AllUsers",
        }
        try:
            await self._post(
                f"/services/data/{_API_VERSION}/sobjects/ContentDocumentLink",
                link_payload,
            )
        except CrmError:
            # The note itself exists; re-raise so the caller sees the link
            # failure rather than silently orphaning the note.
            raise
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
        """Create a Salesforce ``Task`` record (the standard activity
        object). ``WhatId`` links the task to an Opportunity/Account;
        ``WhoId`` links to a Contact.
        """
        body: Dict[str, Any] = {
            "Subject": _truncate(subject, 255),
            "Type": activity_type,
            "Status": "Not Started",
            "Priority": "Normal",
        }
        if due_date:
            body["ActivityDate"] = due_date
        if note:
            body["Description"] = _truncate(note, 32000)
        if deal_external_id:
            body["WhatId"] = deal_external_id
        if contact_external_id:
            body["WhoId"] = contact_external_id
        data = await self._post(
            f"/services/data/{_API_VERSION}/sobjects/Task", body
        )
        task_id = data.get("id")
        if not task_id:
            raise CrmError("Salesforce task response missing id")
        return str(task_id)

    async def update_deal_stage(
        self,
        *,
        deal_external_id: str,
        stage_external_id: str,
    ) -> None:
        await self._patch(
            f"/services/data/{_API_VERSION}/sobjects/Opportunity/{deal_external_id}",
            {"StageName": stage_external_id},
        )

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request("POST", path, json_body=body)
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._request("POST", path, json_body=body)
        if resp.status_code == 401:
            raise CrmAuthError("Salesforce rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("Salesforce rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"Salesforce POST {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.content else {}

    async def _patch(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self._request("PATCH", path, json_body=body)
        if resp.status_code == 401 and self._refresh_token:
            await self._refresh_access_token()
            resp = await self._request("PATCH", path, json_body=body)
        if resp.status_code == 401:
            raise CrmAuthError("Salesforce rejected the token after refresh")
        if resp.status_code == 429:
            raise CrmRateLimitError("Salesforce rate limit hit")
        if resp.status_code >= 400:
            raise CrmError(
                f"Salesforce PATCH {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.content else {}

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

    async def _request(
        self,
        method: str,
        path: str,
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
            f"{self._instance_url}{path}",
            headers=headers,
            json=json_body,
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


# ── Helpers ──────────────────────────────────────────────────────────


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _base64_utf8(text: str) -> str:
    """Salesforce ``ContentNote.Content`` field is Base64-encoded HTML.
    Wrap the plain text in a ``<p>…</p>`` so rendering stays readable
    in the native UI."""
    import base64

    safe = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"<p>{safe}</p>"
    return base64.b64encode(html.encode("utf-8")).decode("ascii")


__all__ = ["SalesforceAdapter"]
