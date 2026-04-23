"""Outbound webhook dispatcher.

Two-stage delivery:

1. ``emit_event(tenant_id, event, payload)`` — synchronously writes a
   ``WebhookDelivery`` row per matching Webhook (``pending`` status) and
   kicks off the Celery delivery task for each. The hot path doesn't
   block on HTTP, so callers (API handlers, Celery tasks) stay cheap.
2. ``deliver_one(delivery_id)`` (invoked from Celery) performs the actual
   HTTP POST, HMAC-signs the payload with the webhook's secret, and
   updates the delivery row with status + next_retry_at on failure.

Retry policy: 5 attempts with exponential backoff (10s, 1m, 5m, 30m, 2h).
After the 5th failure the row is marked ``dead_letter`` and the webhook's
``consecutive_failures`` counter is incremented for the admin UI.

Signature: ``X-Linda-Signature: sha256=<hex>`` over the literal
request body. Recipients verify by recomputing ``HMAC_SHA256(secret,
body)``.

Event filtering: each Webhook row's ``events`` list either contains
``"*"`` (receive everything) or an explicit list of event names. We
match both exact names and (for fan-out convenience) the ``prefix.*``
form, so a webhook subscribing to ``"customer.*"`` receives all
customer.* events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Webhook, WebhookDelivery

logger = logging.getLogger(__name__)


# Backoff schedule in seconds — indexed by current attempt count (0-based).
_BACKOFF_SECONDS: List[int] = [10, 60, 300, 1800, 7200]
_MAX_ATTEMPTS = len(_BACKOFF_SECONDS)


def sign_payload(payload: str, secret: str) -> str:
    """HMAC-SHA256 hex digest of ``payload`` using ``secret``."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _event_matches(filters: List[str], event: str) -> bool:
    """Match an event name against a webhook's ``events`` list.

    - ``"*"`` matches anything.
    - ``"customer.*"`` matches ``customer.<anything>``.
    - Otherwise an exact string match.
    """
    if not filters:
        return False
    if "*" in filters:
        return True
    if event in filters:
        return True
    for f in filters:
        if f.endswith(".*"):
            prefix = f[:-2]
            if event.startswith(prefix + "."):
                return True
    return False


async def emit_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    event: str,
    payload: Dict[str, Any],
    *,
    dispatch_now: bool = True,
) -> List[WebhookDelivery]:
    """Write a ``WebhookDelivery`` row per matching active webhook.

    When ``dispatch_now`` is True (default), we also enqueue the Celery
    delivery task so the HTTP POST fires right away. Set False for bulk
    replay / tests where you want to control timing yourself.

    Safe to call in any request or task handler — DB writes only, no
    blocking HTTP. Returns the delivery rows so callers can log or react.
    """
    stmt = select(Webhook).where(
        Webhook.tenant_id == tenant_id,
        Webhook.active.is_(True),
    )
    webhooks = list((await db.execute(stmt)).scalars().all())

    deliveries: List[WebhookDelivery] = []
    full_payload = _envelope(event, tenant_id, payload)

    for wh in webhooks:
        if not _event_matches(list(wh.events or []), event):
            continue
        delivery = WebhookDelivery(
            webhook_id=wh.id,
            tenant_id=tenant_id,
            event=event,
            payload=full_payload,
            status="pending",
            attempts=[],
            attempt_count=0,
        )
        db.add(delivery)
        deliveries.append(delivery)

    if deliveries:
        await db.flush()

    if dispatch_now:
        for d in deliveries:
            try:
                from backend.app.tasks import webhook_deliver

                webhook_deliver.delay(str(d.id))
            except Exception:
                # Celery not available (local dev, tests) — the row is
                # still in the DB and a future retry sweep will pick it up.
                logger.debug(
                    "Failed to enqueue webhook_deliver for %s", d.id, exc_info=True
                )
    return deliveries


async def deliver_one(db: AsyncSession, delivery_id: uuid.UUID) -> Dict[str, Any]:
    """Attempt one HTTP delivery for a ``WebhookDelivery`` row.

    Updates the row with attempt metadata. Returns a small summary dict
    so the calling Celery task can log the outcome.
    """
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return {"status": "missing", "id": str(delivery_id)}
    if delivery.status in ("sent", "dead_letter"):
        return {"status": delivery.status, "id": str(delivery_id)}

    webhook = await db.get(Webhook, delivery.webhook_id)
    if webhook is None or not webhook.active:
        delivery.status = "dead_letter"
        delivery.last_error = "webhook row missing or disabled"
        return {"status": "dead_letter", "id": str(delivery_id)}

    payload_str = json.dumps(delivery.payload, separators=(",", ":"), default=str)
    signature = sign_payload(payload_str, webhook.secret)

    headers = {
        "Content-Type": "application/json",
        "X-Linda-Event": delivery.event,
        "X-Linda-Signature": f"sha256={signature}",
        "X-Linda-Delivery": str(delivery.id),
        "X-Linda-Attempt": str(delivery.attempt_count + 1),
    }

    now = datetime.now(timezone.utc)
    attempts = list(delivery.attempts or [])
    status_code: Optional[int] = None
    error: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                webhook.url,
                content=payload_str,
                headers=headers,
            )
        status_code = response.status_code
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    except Exception as exc:  # pragma: no cover — defensive
        error = f"{type(exc).__name__}: {exc}"[:500]

    delivery.attempt_count += 1
    delivery.last_status_code = status_code
    delivery.last_error = error
    attempts.append(
        {
            "at": now.isoformat(),
            "status_code": status_code,
            "error": error,
        }
    )
    delivery.attempts = attempts

    # 2xx = success. Anything else (or no status_code because the request
    # errored) counts as a failure for retry purposes.
    if status_code is not None and 200 <= status_code < 300:
        delivery.status = "sent"
        delivery.delivered_at = now
        delivery.next_retry_at = None
        webhook.last_delivered_at = now
        webhook.consecutive_failures = 0
        return {"status": "sent", "id": str(delivery.id), "status_code": status_code}

    # Failed attempt — decide retry or dead-letter.
    if delivery.attempt_count >= _MAX_ATTEMPTS:
        delivery.status = "dead_letter"
        delivery.next_retry_at = None
        webhook.last_failure_at = now
        webhook.consecutive_failures = (webhook.consecutive_failures or 0) + 1
        return {
            "status": "dead_letter",
            "id": str(delivery.id),
            "status_code": status_code,
            "error": error,
        }

    # Schedule the next retry. Celery handles the actual sleep via countdown.
    next_delay = _BACKOFF_SECONDS[delivery.attempt_count]
    delivery.status = "pending"
    delivery.next_retry_at = now + timedelta(seconds=next_delay)
    webhook.last_failure_at = now
    webhook.consecutive_failures = (webhook.consecutive_failures or 0) + 1

    # Kick the retry task now; the caller (Celery) may also do this itself.
    try:
        from backend.app.tasks import webhook_deliver

        webhook_deliver.apply_async(
            args=[str(delivery.id)],
            countdown=next_delay,
        )
    except Exception:
        logger.debug("Failed to schedule webhook retry", exc_info=True)

    return {
        "status": "retrying",
        "id": str(delivery.id),
        "status_code": status_code,
        "error": error,
        "next_retry_in": next_delay,
    }


def _envelope(
    event: str, tenant_id: uuid.UUID, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Wrap the caller-provided payload in a consistent outer envelope.

    Keeps receivers decoupled from whether we add fields like ``event``
    or ``tenant_id`` — they can read off the envelope.
    """
    return {
        "event": event,
        "tenant_id": str(tenant_id),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }


# ── Backwards-compat class API used by the existing api/webhooks.py ──


class WebhookDispatcher:
    """Legacy class wrapper retained for the test-ping endpoint and other
    callers that invoked ``.dispatch()`` / ``.sign_payload()``. New code
    should use the module-level functions above instead."""

    async def dispatch(
        self,
        tenant_id,
        event: str,
        payload: dict,
        db: AsyncSession,
    ) -> None:
        """Enqueue a delivery for every active webhook subscribed to ``event``."""
        await emit_event(db, uuid.UUID(str(tenant_id)), event, payload)

    def sign_payload(self, payload: str, secret: str) -> str:
        return sign_payload(payload, secret)
