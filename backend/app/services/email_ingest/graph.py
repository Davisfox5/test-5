"""Microsoft Graph fetcher — Outlook/O365 inbox + sent items.

Uses the ``/me/mailFolders/{folder}/messages/delta`` endpoint where
available (persisted in ``EmailSyncCursor.delta_link``), falling back
to a plain top-N listing on first sync or when the delta link has
expired.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import httpx

from backend.app.models import EmailSyncCursor, Integration
from backend.app.services.email_ingest.ingest import NormalizedEmail

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="text"',
    }


def _normalize(raw: dict, agent_email: Optional[str], direction: str) -> NormalizedEmail:
    sender = (raw.get("from") or {}).get("emailAddress", {})
    to = [r["emailAddress"]["address"] for r in raw.get("toRecipients", []) if r.get("emailAddress")]
    cc = [r["emailAddress"]["address"] for r in raw.get("ccRecipients", []) if r.get("emailAddress")]
    body = (raw.get("body") or {}).get("content", "") or raw.get("bodyPreview", "")

    internet_headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in raw.get("internetMessageHeaders", []) or []
    }

    references: List[str] = []
    ref_header = internet_headers.get("references", "")
    if ref_header:
        references = ref_header.split()

    received = raw.get("receivedDateTime")
    try:
        received_at = datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None
    except (TypeError, ValueError):
        received_at = None

    return NormalizedEmail(
        provider="microsoft",
        provider_message_id=raw.get("id", ""),
        message_id=raw.get("internetMessageId") or raw.get("id", ""),
        in_reply_to=internet_headers.get("in-reply-to"),
        references=references,
        subject=raw.get("subject"),
        from_address=sender.get("address", ""),
        to_addresses=to,
        cc_addresses=cc,
        body_text=body,
        headers=internet_headers,
        received_at=received_at,
        direction=direction,
        agent_email=agent_email,
    )


def fetch_recent(
    integration: Integration,
    cursor: Optional[EmailSyncCursor],
    access_token: str,
    agent_email: Optional[str],
    max_messages: int = 50,
) -> Iterable[NormalizedEmail]:
    """Yield normalized emails from Inbox + SentItems."""
    with httpx.Client(timeout=20, headers=_headers(access_token)) as client:
        for folder, direction in (("Inbox", "inbound"), ("SentItems", "outbound")):
            url = cursor.delta_link if (cursor and cursor.delta_link and folder == "Inbox") else (
                f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"
                f"?$top={max_messages}&$orderby=receivedDateTime desc"
            )
            while url:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.exception("Graph fetch failed for folder=%s url=%s", folder, url)
                    break
                data = resp.json()
                for raw in data.get("value", []):
                    yield _normalize(raw, agent_email, direction)
                # Stop after one page in the non-delta fast path.
                next_link = data.get("@odata.nextLink")
                delta_link = data.get("@odata.deltaLink")
                if delta_link and cursor is not None and folder == "Inbox":
                    cursor.delta_link = delta_link
                    cursor.last_synced_at = datetime.now(timezone.utc)
                url = next_link if (cursor and cursor.delta_link) else None
