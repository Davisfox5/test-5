"""Outbound webhook dispatcher — delivers events to tenant-configured endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import List

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.models import Webhook

logger = logging.getLogger(__name__)


def sign_payload(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest of a payload string."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class WebhookDispatcher:
    """Dispatches events to all matching webhooks for a tenant."""

    async def dispatch(
        self,
        tenant_id: str,
        event: str,
        payload: dict,
        db: AsyncSession,
    ) -> None:
        """Load active webhooks for the tenant and deliver the event.

        For each webhook whose event filter matches (or contains ``"*"``),
        HMAC-sign the payload and POST it.  Failures are logged but never
        block the caller.
        """
        stmt = select(Webhook).where(
            Webhook.tenant_id == tenant_id,
            Webhook.active.is_(True),
        )
        result = await db.execute(stmt)
        webhooks: List[Webhook] = list(result.scalars().all())

        for webhook in webhooks:
            if "*" not in webhook.events and event not in webhook.events:
                continue

            await self._deliver(webhook, event, payload)

    async def _deliver(self, webhook: Webhook, event: str, payload: dict) -> None:
        """Send a single webhook delivery with HMAC signature."""
        payload_str = json.dumps(payload, separators=(",", ":"))
        signature = self.sign_payload(payload_str, webhook.secret)

        headers = {
            "X-CallSight-Signature": f"sha256={signature}",
            "X-CallSight-Event": event,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    webhook.url,
                    content=payload_str,
                    headers=headers,
                )
            logger.info(
                "Webhook delivered: webhook_id=%s event=%s status=%d",
                webhook.id,
                event,
                response.status_code,
            )
        except Exception:
            logger.exception(
                "Webhook delivery failed: webhook_id=%s event=%s url=%s",
                webhook.id,
                event,
                webhook.url,
            )

    def sign_payload(self, payload: str, secret: str) -> str:
        """Backwards-compatible instance method."""
        return sign_payload(payload, secret)


# ── Synchronous helper for Celery tasks ─────────────────────────────────


def dispatch_sync(session: Session, tenant_id, event: str, payload: dict) -> int:
    """Fire all matching webhooks synchronously. Returns the number delivered.

    Failures are swallowed per-webhook so one bad target never blocks the
    pipeline.  Used from inside Celery workers where an AsyncSession +
    event loop isn't available.
    """
    webhooks: List[Webhook] = (
        session.query(Webhook)
        .filter(Webhook.tenant_id == tenant_id, Webhook.active.is_(True))
        .all()
    )
    delivered = 0
    payload_str = json.dumps(payload, separators=(",", ":"), default=str)

    for webhook in webhooks:
        if "*" not in webhook.events and event not in webhook.events:
            continue
        signature = sign_payload(payload_str, webhook.secret)
        headers = {
            "X-CallSight-Signature": f"sha256={signature}",
            "X-CallSight-Event": event,
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(
                webhook.url, content=payload_str, headers=headers, timeout=10.0
            )
            logger.info(
                "Webhook delivered (sync): id=%s event=%s status=%d",
                webhook.id, event, resp.status_code,
            )
            delivered += 1
        except Exception:
            logger.exception(
                "Webhook delivery failed (sync): id=%s event=%s url=%s",
                webhook.id, event, webhook.url,
            )
    return delivered
