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
from backend.app.services.email_ingest.ingest import NormalizedEmail

logger = logging.getLogger(__name__)


def _credentials(access_token: str) -> Credentials:
    # Scopes are not required here — we only need the token for a single request.
    return Credentials(token=access_token)


def _decode_body(payload: dict) -> str:
    """Walk the MIME tree and return the first text/plain body, else the first text/html."""
    def _walk(part: dict) -> Optional[str]:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime.startswith("text/"):
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                return decoded.decode("utf-8", errors="replace")
            except Exception:
                return None
        for child in part.get("parts", []) or []:
            got = _walk(child)
            if got:
                return got
        return None

    return _walk(payload) or ""


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


def _normalize(raw: dict, agent_email: Optional[str], direction: str) -> NormalizedEmail:
    headers = {h["name"]: h["value"] for h in raw.get("payload", {}).get("headers", [])}
    msg_id = headers.get("Message-ID") or headers.get("Message-Id") or raw.get("id", "")
    references = headers.get("References", "").split() if headers.get("References") else []
    return NormalizedEmail(
        provider="gmail",
        provider_message_id=raw.get("id", ""),
        message_id=msg_id,
        in_reply_to=headers.get("In-Reply-To"),
        references=references,
        subject=headers.get("Subject"),
        from_address=(_addresses(headers.get("From", "")) or [""])[0],
        to_addresses=_addresses(headers.get("To", "")),
        cc_addresses=_addresses(headers.get("Cc", "")),
        body_text=_decode_body(raw.get("payload", {})),
        headers={k.lower(): v for k, v in headers.items()},
        received_at=_received_at(headers.get("Date", "")),
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
        yield _normalize(raw, agent_email, direction)

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
