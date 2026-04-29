"""Outbound webhook management endpoints — MSPs configure where to receive events."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Tenant, Webhook, WebhookDelivery
from backend.app.services.token_crypto import decrypt_token, encrypt_token
from backend.app.services.webhook_dispatcher import WebhookDispatcher
from backend.app.services.webhook_events import WEBHOOK_EVENTS

router = APIRouter()


def _validate_events(events: List[str]) -> List[str]:
    """Reject typos against the canonical event catalog.

    Tenants who saved ``interaction.outcom_inferred`` (typo) currently get
    a webhook that never fires with no error. Validate against the same
    table the ``/webhooks/events`` endpoint exposes so the UI catches
    typos at submit time. ``*`` is the wildcard meaning "all events".
    """
    allowed = set(WEBHOOK_EVENTS.keys()) | {"*"}
    invalid = [e for e in events if e not in allowed]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown event name(s): {', '.join(sorted(set(invalid)))}. "
                "See GET /webhooks/events for the catalog."
            ),
        )
    # Dedupe + preserve order. If "*" is present, drop the others — it
    # subsumes them and the dispatcher already short-circuits on it.
    seen: List[str] = []
    if "*" in events:
        return ["*"]
    for e in events:
        if e not in seen:
            seen.append(e)
    return seen


def _validate_url(url: str) -> str:
    """Reject loopback / RFC1918 / link-local URLs to mitigate SSRF.

    Webhook delivery POSTs from inside the API process; a tenant that
    sets ``http://169.254.169.254/`` could exfiltrate metadata-service
    creds. ``http://`` is allowed (some self-hosted dev setups need it),
    but the host must resolve outside the private ranges.
    """
    from backend.app.services.webhook_dispatcher import is_safe_webhook_url

    cleaned = (url or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="webhook URL is required")
    if not is_safe_webhook_url(cleaned):
        raise HTTPException(
            status_code=400,
            detail=(
                "Webhook URL must be a publicly reachable https/http URL — "
                "loopback, link-local, and RFC1918 addresses are rejected."
            ),
        )
    return cleaned


# ── Pydantic Schemas ────────────────────────────────────


class WebhookCreate(BaseModel):
    url: str
    events: List[str] = Field(default_factory=lambda: ["*"])
    active: bool = True


class WebhookOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    url: str
    events: List[str]
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookCreateResponse(BaseModel):
    """Returned on creation — includes the HMAC secret shown only once."""
    id: uuid.UUID
    tenant_id: uuid.UUID
    url: str
    events: List[str]
    active: bool
    secret: str = Field(..., description="HMAC secret — save it now, it will not be shown again")
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookUpdate(BaseModel):
    url: Optional[str] = None
    events: Optional[List[str]] = None
    active: Optional[bool] = None


class WebhookTestResponse(BaseModel):
    status: str
    status_code: Optional[int] = None
    error: Optional[str] = None


# ── Endpoints ───────────────────────────────────────────


@router.get("/webhooks", response_model=List[WebhookOut])
async def list_webhooks(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """List all webhooks for the current tenant."""
    stmt = (
        select(Webhook)
        .where(Webhook.tenant_id == tenant.id)
        .order_by(Webhook.created_at.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/webhooks", response_model=WebhookCreateResponse, status_code=201)
async def create_webhook(
    body: WebhookCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Create a new webhook. The HMAC secret is returned once in the response."""
    safe_url = _validate_url(body.url)
    safe_events = _validate_events(body.events)
    hmac_secret = secrets.token_urlsafe(32)

    # Store the HMAC secret encrypted at rest (Fernet) so a leaked DB
    # backup can't be replayed against tenants' webhook receivers. The
    # plaintext is returned exactly once in the response below.
    webhook = Webhook(
        tenant_id=tenant.id,
        url=safe_url,
        events=safe_events,
        secret=encrypt_token(hmac_secret) or hmac_secret,
        active=body.active,
    )
    db.add(webhook)
    await db.flush()

    return WebhookCreateResponse(
        id=webhook.id,
        tenant_id=webhook.tenant_id,
        url=webhook.url,
        events=webhook.events,
        active=webhook.active,
        secret=hmac_secret,
        created_at=webhook.created_at,
    )


@router.patch("/webhooks/{webhook_id}", response_model=WebhookOut)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Update a webhook's URL, events, or active status."""
    stmt = select(Webhook).where(
        Webhook.id == webhook_id,
        Webhook.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    webhook = result.scalar_one_or_none()

    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")

    if body.url is not None:
        webhook.url = _validate_url(body.url)
    if body.events is not None:
        webhook.events = _validate_events(body.events)
    if body.active is not None:
        webhook.active = body.active

    await db.flush()
    return webhook


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Delete a webhook."""
    stmt = select(Webhook).where(
        Webhook.id == webhook_id,
        Webhook.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    webhook = result.scalar_one_or_none()

    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")

    await db.delete(webhook)


@router.post("/webhooks/{webhook_id}/test", response_model=WebhookTestResponse)
async def test_webhook(
    webhook_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Send a test ping to the webhook URL with an HMAC signature."""
    stmt = select(Webhook).where(
        Webhook.id == webhook_id,
        Webhook.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    webhook = result.scalar_one_or_none()

    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")

    dispatcher = WebhookDispatcher()
    test_payload = {
        "event": "webhook.test",
        "webhook_id": str(webhook.id),
        "tenant_id": str(webhook.tenant_id),
        "message": "This is a test ping from LINDA.",
    }

    import json
    payload_str = json.dumps(test_payload, separators=(",", ":"))
    # ``Webhook.secret`` is Fernet-encrypted at rest; decrypt before
    # signing. ``decrypt_token`` is tolerant of legacy plaintext rows.
    plaintext_secret = decrypt_token(webhook.secret) or webhook.secret
    signature = dispatcher.sign_payload(payload_str, plaintext_secret)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                webhook.url,
                json=test_payload,
                headers={
                    "X-Linda-Signature": f"sha256={signature}",
                    "X-Linda-Event": "webhook.test",
                    "Content-Type": "application/json",
                },
            )
        return WebhookTestResponse(
            status="delivered",
            status_code=response.status_code,
        )
    except httpx.HTTPError as exc:
        return WebhookTestResponse(
            status="failed",
            error=str(exc),
        )


# ── Event catalog + delivery log ─────────────────────────────────────


@router.get("/webhooks/events")
async def list_webhook_events() -> dict:
    """Return the catalog of supported event names + descriptions.

    Used by the admin UI to render the "which events should this webhook
    receive?" picker. ``*`` is a wildcard that receives everything.
    """
    return {
        "events": [
            {"name": name, "description": desc}
            for name, desc in WEBHOOK_EVENTS.items()
        ]
    }


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    webhook_id: uuid.UUID
    event: str
    status: str
    attempt_count: int
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get(
    "/webhooks/{webhook_id}/deliveries",
    response_model=List[WebhookDeliveryOut],
)
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Recent deliveries for one webhook, newest first. Useful when a tenant
    is debugging receiver-side issues ("why didn't my endpoint hear about
    this?")."""
    # Enforce tenant scope on the webhook row first.
    wh = await db.get(Webhook, webhook_id)
    if wh is None or wh.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Webhook not found")

    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    return list((await db.execute(stmt)).scalars().all())
