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

import base64
import html as html_mod
import re

import httpx

from backend.app.models import EmailSyncCursor, Integration
from backend.app.services.email_ingest.ingest import (
    NormalizedAttachment,
    NormalizedEmail,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _headers(access_token: str) -> dict:
    # Ask for HTML bodies so we can render them.  Strip to text for
    # the analysis pipeline via _strip_html_to_text.
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="html"',
    }


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")


def _strip_html_to_text(html: str) -> str:
    if not html:
        return ""
    # Kill scripts/styles wholesale, then strip the rest.
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html_mod.unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def _normalize(
    raw: dict,
    agent_email: Optional[str],
    direction: str,
    access_token: Optional[str] = None,
) -> NormalizedEmail:
    sender = (raw.get("from") or {}).get("emailAddress", {})
    to = [r["emailAddress"]["address"] for r in raw.get("toRecipients", []) if r.get("emailAddress")]
    cc = [r["emailAddress"]["address"] for r in raw.get("ccRecipients", []) if r.get("emailAddress")]
    bcc = [r["emailAddress"]["address"] for r in raw.get("bccRecipients", []) if r.get("emailAddress")]

    body_obj = raw.get("body") or {}
    body_content = body_obj.get("content", "") or ""
    content_type = (body_obj.get("contentType") or "").lower()

    if content_type == "html" and body_content:
        body_html: Optional[str] = body_content
        body_text = _strip_html_to_text(body_content) or raw.get("bodyPreview", "")
    else:
        body_html = None
        body_text = body_content or raw.get("bodyPreview", "")

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

    provider_mid = raw.get("id", "")

    # Attachment metadata (list): Graph tells us has_attachments up front.
    attachments: List[NormalizedAttachment] = []
    if raw.get("hasAttachments") and access_token:
        attachments = _list_attachments(access_token, provider_mid)

    def _lazy_fetch(att: NormalizedAttachment) -> Optional[bytes]:
        if att.data is not None:
            return att.data
        if not access_token or not att.provider_attachment_id:
            return None
        try:
            with httpx.Client(
                timeout=20, headers={"Authorization": f"Bearer {access_token}"}
            ) as client:
                resp = client.get(
                    f"{GRAPH_BASE}/me/messages/{provider_mid}/attachments/{att.provider_attachment_id}"
                )
                resp.raise_for_status()
                payload = resp.json()
            if payload.get("@odata.type") == "#microsoft.graph.fileAttachment":
                return base64.b64decode(payload.get("contentBytes", ""))
        except Exception:
            logger.exception(
                "Graph attachment fetch failed message=%s att=%s",
                provider_mid, att.provider_attachment_id,
            )
        return None

    return NormalizedEmail(
        provider="microsoft",
        provider_message_id=provider_mid,
        message_id=raw.get("internetMessageId") or provider_mid,
        in_reply_to=internet_headers.get("in-reply-to"),
        references=references,
        subject=raw.get("subject"),
        from_address=sender.get("address", ""),
        to_addresses=to,
        cc_addresses=cc,
        bcc_addresses=bcc,
        body_text=body_text,
        body_html=body_html,
        headers=internet_headers,
        received_at=received_at,
        direction=direction,
        agent_email=agent_email,
        attachments=attachments,
        attachment_fetcher=_lazy_fetch,
    )


def _list_attachments(access_token: str, message_id: str) -> List[NormalizedAttachment]:
    """Enumerate attachment metadata without pulling bytes."""
    out: List[NormalizedAttachment] = []
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=15, headers=headers) as client:
            resp = client.get(
                f"{GRAPH_BASE}/me/messages/{message_id}/attachments"
                f"?$select=id,name,contentType,size,isInline,contentId"
            )
            resp.raise_for_status()
            items = resp.json().get("value", [])
    except httpx.HTTPError:
        logger.exception("Graph attachment list failed for message=%s", message_id)
        return out

    for item in items:
        out.append(NormalizedAttachment(
            filename=item.get("name") or "attachment",
            content_type=item.get("contentType"),
            size_bytes=item.get("size"),
            provider_attachment_id=item.get("id"),
            content_id=item.get("contentId"),
            inline=bool(item.get("isInline")),
            data=None,
        ))
    return out


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
                    yield _normalize(raw, agent_email, direction, access_token=access_token)
                # Stop after one page in the non-delta fast path.
                next_link = data.get("@odata.nextLink")
                delta_link = data.get("@odata.deltaLink")
                if delta_link and cursor is not None and folder == "Inbox":
                    cursor.delta_link = delta_link
                    cursor.last_synced_at = datetime.now(timezone.utc)
                url = next_link if (cursor and cursor.delta_link) else None
