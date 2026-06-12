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
import hmac
import json
import logging
from typing import Optional

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


# ── Google OIDC verification (optional, defense in depth) ─────────────
#
# When GMAIL_PUSH_OIDC_AUDIENCE is configured, Pub/Sub push deliveries
# must carry a Google-signed OIDC JWT (Authorization: Bearer ...) whose
# audience matches. Google's signing keys are fetched from the standard
# JWKS endpoint and cached in-process for an hour.

_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_JWKS_TTL_SECONDS = 3600
_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}


async def _google_jwks() -> dict:
    import time as _time

    now = _time.monotonic()
    if _jwks_cache["keys"] is None or now - _jwks_cache["fetched_at"] > _JWKS_TTL_SECONDS:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_GOOGLE_JWKS_URL)
            resp.raise_for_status()
            _jwks_cache["keys"] = resp.json()
            _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]


async def _verify_pubsub_oidc(request: Request) -> None:
    """Raise 401 unless the request carries a valid Google OIDC token.

    No-op when GMAIL_PUSH_OIDC_AUDIENCE is unset (the ?token= shared
    secret remains the baseline check either way).
    """
    audience = _settings.GMAIL_PUSH_OIDC_AUDIENCE
    if not audience:
        return

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing OIDC token")
    bearer = auth_header[len("Bearer "):]

    from jose import jwt

    try:
        jwks = await _google_jwks()
        claims = jwt.decode(
            bearer,
            jwks,
            algorithms=["RS256"],
            audience=audience,
        )
    except Exception as exc:  # fail closed: bad sig, expired, JWKS fetch error
        logger.warning("Pub/Sub OIDC verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Bad OIDC token")

    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise HTTPException(status_code=401, detail="Bad OIDC issuer")
    expected_sa = _settings.GMAIL_PUSH_OIDC_SERVICE_ACCOUNT
    if expected_sa and not hmac.compare_digest(
        str(claims.get("email") or ""), expected_sa
    ):
        raise HTTPException(status_code=401, detail="Bad OIDC service account")


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
    await _verify_pubsub_oidc(request)
    expected = _settings.GMAIL_PUSH_TOKEN
    if expected and not hmac.compare_digest(token, expected):
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
        if expected_state and not hmac.compare_digest(
            str(n.get("clientState") or ""), expected_state
        ):
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
