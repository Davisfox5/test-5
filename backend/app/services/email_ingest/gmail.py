"""Gmail fetcher — pulls new messages for a connected Integration.

Uses the ``q`` query param with ``after:`` to bound the window and an
internal cursor (EmailSyncCursor.history_id) to avoid re-fetching on
every run.  Fetches messages from both INBOX (inbound) and SENT
(outbound, so we capture human-authored replies) in one pass.
"""

from __future__ import annotations

import base64
import email.utils
import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from backend.app.models import EmailSyncCursor, Integration
from backend.app.services.email_ingest.ingest import (
    NormalizedAttachment,
    NormalizedEmail,
)

logger = logging.getLogger(__name__)


def _credentials(access_token: str) -> Credentials:
    # Scopes are not required here — we only need the token for a single request.
    return Credentials(token=access_token)


def _decode_b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _extract_bodies_and_attachments(
    payload: dict,
) -> tuple[str, Optional[str], List[NormalizedAttachment]]:
    """Walk the MIME tree once, returning plain text, html, and attachments.

    Gmail hands body data for small parts inline (``body.data``) and
    large parts via ``body.attachmentId``.  We capture the first
    text/plain and first text/html we hit, and collect everything else
    as an attachment — with ``data=None`` when it needs a second API
    call, so the caller can fetch on demand post-classification.
    """
    plain_text: Optional[str] = None
    html_text: Optional[str] = None
    atts: List[NormalizedAttachment] = []

    def _walk(part: dict) -> None:
        nonlocal plain_text, html_text
        mime = part.get("mimeType", "") or ""
        body = part.get("body", {}) or {}
        data_inline = body.get("data")
        attachment_id = body.get("attachmentId")
        filename = part.get("filename") or ""

        # Inline headers (Content-ID, Content-Disposition).
        part_headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in part.get("headers", []) or []
        }
        content_id = (part_headers.get("content-id") or "").strip("<>") or None
        disposition = (part_headers.get("content-disposition") or "").lower()
        is_attachment = bool(filename) or "attachment" in disposition
        is_inline = "inline" in disposition and bool(content_id)

        if mime.startswith("multipart/"):
            for child in part.get("parts", []) or []:
                _walk(child)
            return

        if is_attachment or is_inline:
            data: Optional[bytes] = None
            if data_inline:
                try:
                    data = _decode_b64url(data_inline)
                except Exception:
                    data = None
            atts.append(
                NormalizedAttachment(
                    filename=filename or (content_id or "attachment"),
                    content_type=mime or None,
                    size_bytes=body.get("size"),
                    provider_attachment_id=attachment_id,
                    content_id=content_id,
                    inline=is_inline,
                    data=data,
                )
            )
            return

        if mime == "text/plain" and plain_text is None and data_inline:
            try:
                plain_text = _decode_b64url(data_inline).decode("utf-8", errors="replace")
            except Exception:
                plain_text = None
        elif mime == "text/html" and html_text is None and data_inline:
            try:
                html_text = _decode_b64url(data_inline).decode("utf-8", errors="replace")
            except Exception:
                html_text = None

        # Recurse into any child parts (rare at text/* but harmless).
        for child in part.get("parts", []) or []:
            _walk(child)

    _walk(payload)
    return (plain_text or "", html_text, atts)


def _addresses(header_value: str) -> List[str]:
    if not header_value:
        return []
    return [addr for _, addr in email.utils.getaddresses([header_value]) if addr]


def _received_at(header_value: str) -> Optional[datetime]:
    if not header_value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(header_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _normalize(
    raw: dict,
    agent_email: Optional[str],
    direction: str,
    service=None,
) -> NormalizedEmail:
    headers = {h["name"]: h["value"] for h in raw.get("payload", {}).get("headers", [])}
    msg_id = headers.get("Message-ID") or headers.get("Message-Id") or raw.get("id", "")
    references = headers.get("References", "").split() if headers.get("References") else []
    body_text, body_html, attachments = _extract_bodies_and_attachments(
        raw.get("payload", {})
    )

    provider_mid = raw.get("id", "")

    def _lazy_fetch(att: NormalizedAttachment) -> Optional[bytes]:
        """Fetch attachment bytes on demand (after classification)."""
        if att.data is not None:
            return att.data
        if service is None or not att.provider_attachment_id:
            return None
        try:
            resp = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=provider_mid, id=att.provider_attachment_id)
                .execute()
            )
            data = resp.get("data")
            if not data:
                return None
            return _decode_b64url(data)
        except Exception:
            logger.exception("Gmail attachment fetch failed id=%s", att.provider_attachment_id)
            return None

    return NormalizedEmail(
        provider="gmail",
        provider_message_id=provider_mid,
        message_id=msg_id,
        in_reply_to=headers.get("In-Reply-To"),
        references=references,
        subject=headers.get("Subject"),
        from_address=(_addresses(headers.get("From", "")) or [""])[0],
        to_addresses=_addresses(headers.get("To", "")),
        cc_addresses=_addresses(headers.get("Cc", "")),
        bcc_addresses=_addresses(headers.get("Bcc", "")),
        body_text=body_text,
        body_html=body_html,
        headers={k.lower(): v for k, v in headers.items()},
        received_at=_received_at(headers.get("Date", "")),
        direction=direction,
        agent_email=agent_email,
        attachments=attachments,
        attachment_fetcher=_lazy_fetch,
    )


def fetch_recent(
    integration: Integration,
    cursor: Optional[EmailSyncCursor],
    access_token: str,
    agent_email: Optional[str],
    max_messages: int = 50,
) -> Iterable[NormalizedEmail]:
    """Yield normalized emails newer than the cursor's historyId (if any)."""
    service = build("gmail", "v1", credentials=_credentials(access_token), cache_discovery=False)

    if cursor and cursor.history_id:
        # Incremental — use history API.
        try:
            history = (
                service.users()
                .history()
                .list(userId="me", startHistoryId=cursor.history_id, historyTypes=["messageAdded"])
                .execute()
            )
            message_ids = [
                m["message"]["id"]
                for h in history.get("history", [])
                for m in h.get("messagesAdded", [])
            ]
            cursor.history_id = history.get("historyId") or cursor.history_id
        except Exception:
            logger.exception("Gmail history fetch failed; falling back to recent list")
            message_ids = _recent_message_ids(service, max_messages)
    else:
        message_ids = _recent_message_ids(service, max_messages)

    for mid in message_ids[:max_messages]:
        raw = service.users().messages().get(userId="me", id=mid, format="full").execute()
        labels = set(raw.get("labelIds", []))
        direction = "outbound" if "SENT" in labels else "inbound"
        yield _normalize(raw, agent_email, direction, service=service)

    # Always advance the historyId to the latest profile value to keep windows tight.
    if cursor is not None:
        try:
            profile = service.users().getProfile(userId="me").execute()
            cursor.history_id = str(profile.get("historyId") or cursor.history_id or "")
            cursor.last_synced_at = datetime.now(timezone.utc)
        except Exception:
            logger.exception("Failed to advance Gmail historyId cursor")


def _recent_message_ids(service, limit: int) -> List[str]:
    """List message ids from INBOX and SENT, capped at ``limit`` per label."""
    ids: List[str] = []
    for label in ("INBOX", "SENT"):
        resp = (
            service.users()
            .messages()
            .list(userId="me", labelIds=[label], maxResults=limit)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
    return ids
