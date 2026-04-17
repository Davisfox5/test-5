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
from backend.app.models import Tenant, Webhook
from backend.app.services.webhook_dispatcher import WebhookDispatcher

router = APIRouter()


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
    hmac_secret = secrets.token_urlsafe(32)

    webhook = Webhook(
        tenant_id=tenant.id,
        url=body.url,
        events=body.events,
        secret=hmac_secret,
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
        webhook.url = body.url
    if body.events is not None:
        webhook.events = body.events
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
        "message": "This is a test ping from CallSight AI.",
    }

    import json
    payload_str = json.dumps(test_payload, separators=(",", ":"))
    signature = dispatcher.sign_payload(payload_str, webhook.secret)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                webhook.url,
                json=test_payload,
                headers={
                    "X-CallSight-Signature": f"sha256={signature}",
                    "X-CallSight-Event": "webhook.test",
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
