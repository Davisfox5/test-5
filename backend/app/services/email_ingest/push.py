"""Fetch specific messages on demand — driven by push notifications.

The poller in :mod:`poller` pulls windows; this module instead gets
individual message IDs from a Pub/Sub (Gmail) or Graph webhook and
normalizes them through the same :func:`ingest_email` path.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from backend.app.models import EmailSyncCursor, Integration
from backend.app.services.email_ingest.gmail import _normalize as _normalize_gmail
from backend.app.services.email_ingest.graph import _normalize as _normalize_graph
from backend.app.services.email_ingest.ingest import NormalizedEmail

logger = logging.getLogger(__name__)


# ── Gmail ────────────────────────────────────────────────


def fetch_gmail_since_history(
    access_token: str,
    start_history_id: str,
    agent_email: Optional[str],
) -> Iterable[NormalizedEmail]:
    """Yield every message added since ``start_history_id``.

    Gmail's push payload only tells us "something changed" + the latest
    historyId; it's our job to diff from the cursor forward.
    """
    service = build(
        "gmail", "v1", credentials=Credentials(token=access_token), cache_discovery=False
    )
    page_token: Optional[str] = None
    seen: set[str] = set()
    while True:
        req = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            pageToken=page_token,
        )
        try:
            resp = req.execute()
        except Exception:
            logger.exception("Gmail history diff failed (start=%s)", start_history_id)
            return
        for h in resp.get("history", []):
            for added in h.get("messagesAdded", []):
                mid = added.get("message", {}).get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                try:
                    raw = (
                        service.users()
                        .messages()
                        .get(userId="me", id=mid, format="full")
                        .execute()
                    )
                except Exception:
                    logger.exception("Gmail message fetch failed id=%s", mid)
                    continue
                labels = set(raw.get("labelIds", []))
                direction = "outbound" if "SENT" in labels else "inbound"
                yield _normalize_gmail(raw, agent_email, direction, service=service)
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def watch_gmail(access_token: str, topic_name: str) -> dict:
    """Register a Gmail watch → Pub/Sub push for this mailbox.

    Google expires the watch after ~7 days; callers are expected to
    re-register on a schedule.  Returns the response so the caller can
    persist the new ``historyId``.
    """
    service = build(
        "gmail", "v1", credentials=Credentials(token=access_token), cache_discovery=False
    )
    return service.users().watch(
        userId="me",
        body={
            "topicName": topic_name,
            # Listen on both inbox + sent so we capture agent replies too.
            "labelIds": ["INBOX", "SENT"],
            "labelFilterBehavior": "INCLUDE",
        },
    ).execute()


# ── Microsoft Graph ──────────────────────────────────────


def fetch_graph_message(
    access_token: str,
    message_id: str,
    agent_email: Optional[str],
    direction_hint: Optional[str] = None,
) -> Optional[NormalizedEmail]:
    """Fetch one Graph message by id."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="html"',
    }
    try:
        with httpx.Client(timeout=15, headers=headers) as client:
            resp = client.get(f"https://graph.microsoft.com/v1.0/me/messages/{message_id}")
            resp.raise_for_status()
            raw = resp.json()
    except httpx.HTTPError:
        logger.exception("Graph message fetch failed id=%s", message_id)
        return None

    direction = direction_hint or _infer_graph_direction(raw, agent_email)
    return _normalize_graph(raw, agent_email, direction, access_token=access_token)


def _infer_graph_direction(raw: dict, agent_email: Optional[str]) -> str:
    """Guess direction from sender when the notification didn't tell us."""
    sender = ((raw.get("from") or {}).get("emailAddress") or {}).get("address", "")
    if agent_email and sender.lower() == agent_email.lower():
        return "outbound"
    return "inbound"


def subscribe_graph_mailbox(
    access_token: str,
    notification_url: str,
    client_state: str,
    lifetime_minutes: int = 60 * 24 * 2,  # Graph max for messages is ~3 days
) -> dict:
    """Register a Graph change-notification subscription for this mailbox.

    Creates a single subscription covering messages in all folders; on
    delivery we inspect ``parentFolderId`` (Inbox vs SentItems) to set
    direction.  Graph requires the subscription URL to respond to a
    validation challenge inside 10 seconds — the endpoint below handles
    that.
    """
    from datetime import datetime, timedelta, timezone

    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=lifetime_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    body = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": "me/messages",
        "expirationDateTime": expires,
        "clientState": client_state,
        "latestSupportedTlsVersion": "v1_2",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()
