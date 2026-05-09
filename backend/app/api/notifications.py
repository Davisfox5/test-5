"""Notifications API — per-user inbox + read tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_scope,
)
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Notification, Tenant
from backend.app.services.notifications import (
    mark_all_read,
    mark_read,
    notification_channel,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class NotificationOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    title: str
    body: Optional[str]
    link_url: Optional[str]
    action_item_id: Optional[uuid.UUID]
    interaction_id: Optional[uuid.UUID]
    is_read: bool
    read_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationList(BaseModel):
    items: List[NotificationOut]
    unread_count: int


@router.get(
    "/notifications",
    response_model=NotificationList,
    dependencies=[Depends(require_scope("notifications:read"))],
)
async def list_notifications(
    only_unread: bool = Query(False, description="Filter to unread only"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Return the current user's notifications, newest first."""
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")

    stmt = (
        select(Notification)
        .where(
            Notification.user_id == principal.user.id,
            Notification.tenant_id == tenant.id,
        )
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if only_unread:
        stmt = stmt.where(Notification.is_read.is_(False))

    items = list((await db.execute(stmt)).scalars())

    unread = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == principal.user.id,
            Notification.tenant_id == tenant.id,
            Notification.is_read.is_(False),
        )
    )
    unread_count = unread.scalar_one() or 0

    return NotificationList(items=items, unread_count=int(unread_count))


@router.get(
    "/notifications/unread-count",
    response_model=dict,
    dependencies=[Depends(require_scope("notifications:read"))],
)
async def unread_count(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Lightweight endpoint for the notification bell badge."""
    if not principal.user:
        return {"unread_count": 0}
    result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == principal.user.id,
            Notification.tenant_id == tenant.id,
            Notification.is_read.is_(False),
        )
    )
    return {"unread_count": int(result.scalar_one() or 0)}


@router.post(
    "/notifications/{notification_id}/read",
    response_model=dict,
    dependencies=[Depends(require_scope("notifications:write"))],
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")
    ok = await mark_read(db, notification_id=notification_id, user_id=principal.user.id)
    return {"ok": ok}


@router.post(
    "/notifications/mark-all-read",
    response_model=dict,
    dependencies=[Depends(require_scope("notifications:write"))],
)
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")
    n = await mark_all_read(db, user_id=principal.user.id, tenant_id=tenant.id)
    return {"updated": n}


# ── SSE stream — replaces 30s polling on the client ─────────────────────


_SSE_HEARTBEAT_SECONDS = 25.0


async def _notification_sse_stream(
    request: Request, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> AsyncGenerator[bytes, None]:
    """Yield ``text/event-stream`` frames driven by the Redis pub/sub channel.

    Heartbeats every 25s keep proxies (Cloudflare, Fly's edge) from
    timing out idle connections. The stream ends silently when the
    client disconnects.
    """
    # Lazy import so test environments without redis still load this module.
    try:
        import redis.asyncio as aioredis  # type: ignore
    except Exception:  # pragma: no cover
        # Fallback: emit a single comment so the client falls through to
        # its REST polling path.
        yield b": sse-unavailable\n\n"
        return

    settings = get_settings()
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = client.pubsub()
    channel = notification_channel(tenant_id, user_id)
    try:
        await pubsub.subscribe(channel)
        # Initial hello frame so the browser fires `onopen` quickly.
        yield b"event: ready\ndata: {}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True),
                    timeout=_SSE_HEARTBEAT_SECONDS,
                )
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if msg is None:
                yield b": keep-alive\n\n"
                continue
            data = msg.get("data") or "{}"
            yield f"event: notification\ndata: {data}\n\n".encode("utf-8")
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await client.close()
        except Exception:  # pragma: no cover
            logger.debug("SSE teardown failed", exc_info=True)


@router.get(
    "/notifications/stream",
    dependencies=[Depends(require_scope("notifications:read"))],
)
async def notifications_stream(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Server-Sent Events stream of notifications for the current user.

    The frontend uses this to avoid the 30-second polling loop. If
    EventSource is unsupported or the stream errors, the client falls
    back to the existing REST endpoints.
    """
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")

    return StreamingResponse(
        _notification_sse_stream(request, tenant.id, principal.user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable nginx/Cloudflare buffering so events flush immediately.
            "X-Accel-Buffering": "no",
        },
    )
