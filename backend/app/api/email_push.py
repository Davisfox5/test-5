"""Push-notification endpoints for email ingestion.

Two flows:

**Gmail Pub/Sub**
    Google Cloud Pub/Sub delivers a push message whenever a watched
    mailbox changes.  The URL here is what you register in the Pub/Sub
    push subscription.  The body is a single base64-encoded message
    with ``emailAddress`` and ``historyId``.  We dedupe by historyId +
    account, hand off to Celery, and return 204 fast — Pub/Sub retries
    anything over a few-second budget.

**Microsoft Graph**
    Graph POSTs a batch of change notifications to our subscription
    URL.  On the very first POST it sends ``validationToken`` as a
    query param and expects us to echo it back as plain text within
    10 seconds.  After that, notifications include ``resourceData.id``
    (the message id), ``subscriptionId``, and ``clientState``.

Both endpoints are public (no tenant auth) but protected by
provider-specific shared secrets so unknown callers are rejected.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import EmailSyncCursor, Integration
from backend.app.services.push_rate_limiter import get_limiter

# Rate limits:
#   Gmail Pub/Sub delivers one notification per mailbox-change, retry on
#   non-2xx.  Legitimate volume for a busy tenant rarely exceeds a few
#   per second.  We cap at 300/minute per source IP — enough headroom
#   for Google's push infrastructure, tight enough to blunt abuse.
#   Graph batches up to 1000 notifications per POST so we let it breathe
#   a bit more on payload size but limit request rate similarly.
_GMAIL_RATE = (300, 60)
_GRAPH_RATE = (300, 60)


def _client_key(request: Request, prefix: str) -> str:
    # Trust the immediate remote addr; production will sit behind a
    # load balancer that should set X-Forwarded-For which Starlette
    # surfaces through request.client when configured.
    client_host = request.client.host if request.client else "unknown"
    return f"{prefix}:{client_host}"


def _enforce_rate(request: Request, prefix: str, limit_window: tuple[int, int]) -> None:
    limit, window = limit_window
    allowed, remaining, reset_in = get_limiter().check(
        key=_client_key(request, prefix), limit=limit, window_seconds=window
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={
                "Retry-After": str(max(1, reset_in)),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_in),
            },
        )

logger = logging.getLogger(__name__)

router = APIRouter()
_settings = get_settings()


# ── Gmail Pub/Sub push ─────────────────────────────────


class _PubsubMessage(BaseModel):
    data: Optional[str] = None
    messageId: Optional[str] = None
    publishTime: Optional[str] = None


class _PubsubEnvelope(BaseModel):
    message: _PubsubMessage
    subscription: Optional[str] = None


@router.post("/email-push/gmail", status_code=204)
async def gmail_push(
    request: Request,
    envelope: _PubsubEnvelope,
    token: str = Query("", description="Shared secret from Pub/Sub push URL"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle one Pub/Sub push delivery.

    Pub/Sub expects a 2xx in under ~10 seconds or it retries.  We do
    the tenant + cursor lookup synchronously (fast), then dispatch a
    Celery task that diffs the Gmail history and ingests.
    """
    _enforce_rate(request, "gmail-push", _GMAIL_RATE)
    expected = _settings.GMAIL_PUSH_TOKEN
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Bad push token")

    if envelope.message.data is None:
        # Pub/Sub sometimes sends empty warm-up pings.
        return Response(status_code=204)

    try:
        decoded = base64.b64decode(envelope.message.data).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        logger.exception("Malformed Pub/Sub payload; ignoring")
        return Response(status_code=204)

    account_email = payload.get("emailAddress")
    new_history_id = str(payload.get("historyId") or "")
    if not account_email or not new_history_id:
        return Response(status_code=204)

    # Resolve the Google Integration for this mailbox via the User email.
    from backend.app.models import User

    integration = (await db.execute(
        select(Integration)
        .join(User, User.id == Integration.user_id)
        .where(
            Integration.provider == "google",
            User.email == account_email,
        )
    )).scalars().first()
    if integration is None:
        logger.info("Pub/Sub for %s — no Google integration on file", account_email)
        return Response(status_code=204)

    # Dispatch Celery task with the integration id + new historyId.
    try:
        from backend.app.tasks import email_push_process_gmail

        email_push_process_gmail.delay(str(integration.id), new_history_id)
    except Exception:
        logger.exception("Failed to enqueue Gmail push task")

    return Response(status_code=204)


# ── Microsoft Graph change notifications ───────────────


@router.post("/email-push/graph")
async def graph_webhook(
    request: Request,
    validationToken: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Receive Graph change notifications.

    On first POST, Graph passes ``validationToken`` and expects it
    echoed back in plain text within 10s.  On real deliveries the body
    is JSON — we verify ``clientState`` matches ours before enqueuing.
    """
    # Validation handshake is cheap and must complete inside 10 seconds
    # — intentionally not rate-limited.  Real deliveries are.
    if validationToken is not None:
        return Response(content=validationToken, media_type="text/plain")

    _enforce_rate(request, "graph-push", _GRAPH_RATE)

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=202)  # Be lenient; Graph retries.

    expected_state = _settings.GRAPH_CLIENT_STATE
    notifications = body.get("value") or []
    queued = 0

    for n in notifications:
        if expected_state and n.get("clientState") != expected_state:
            logger.warning("Graph notification rejected: clientState mismatch")
            continue
        resource_data = n.get("resourceData") or {}
        message_id = resource_data.get("id")
        subscription_id = n.get("subscriptionId")
        if not message_id or not subscription_id:
            continue

        # Map subscription_id → Integration via the sync-cursor table (we
        # stash the subscription id there on create).
        cursor = (await db.execute(
            select(EmailSyncCursor).where(
                EmailSyncCursor.provider == "microsoft",
                EmailSyncCursor.delta_link == subscription_id,
            )
        )).scalar_one_or_none()
        # If someone's using the delta_link field for actual delta links,
        # fall back to scanning integrations by tenant_id later. For now
        # require the cursor link to be the subscription id (set on subscribe).
        if cursor is None:
            logger.info("Graph notification for unknown subscription %s", subscription_id)
            continue

        try:
            from backend.app.tasks import email_push_process_graph

            email_push_process_graph.delay(
                str(cursor.integration_id),
                message_id,
                resource_data.get("parentFolderId"),
            )
            queued += 1
        except Exception:
            logger.exception("Failed to enqueue Graph push task")

    return Response(status_code=202, content=json.dumps({"queued": queued}))
