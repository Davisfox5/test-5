"""WebSocket ticket endpoint.

Issues a short-lived, single-use ticket so browsers can open a
WebSocket with auth.  See ``docs/SCORING_ARCHITECTURE.md`` for the
overall scheme; see ``backend/app/services/ws_tickets.py`` for the
storage and consume semantics.

Monitor-role tickets additionally require that the requesting user
holds a ``manager`` or ``admin`` role on the tenant.  Because API keys
today aren't user-scoped, callers identify themselves via the
``user_id`` field on the request body; the handler checks the row on
``users`` and refuses if the role doesn't qualify.
"""

from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Tenant, User
from backend.app.services.ws_tickets import (
    DEFAULT_TICKET_TTL_SEC,
    issue_ticket,
)

router = APIRouter()


_MONITOR_ROLES = {"manager", "admin"}


class TicketRequest(BaseModel):
    session_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Session / call ID the ticket should be bound to.  "
        "Generated server-side if omitted.",
    )
    role: Literal["agent", "monitor"] = "agent"
    user_id: Optional[uuid.UUID] = Field(
        default=None,
        description="User requesting the connection.  Required for "
        "``monitor`` role so the server can verify manager/admin.",
    )


class TicketResponse(BaseModel):
    ticket: str
    session_id: str
    role: str
    expires_at: float


async def _get_redis():
    """Yield an async Redis client.  Factored out so tests can override."""
    import redis.asyncio as aioredis

    from backend.app.config import get_settings

    client = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@router.post("/ws/tickets", response_model=TicketResponse, status_code=201)
async def create_ticket(
    body: TicketRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    redis=Depends(_get_redis),
):
    """Issue a single-use ticket for a WebSocket connection."""
    user_id: Optional[str] = None
    if body.role == "monitor":
        if body.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="user_id is required for monitor tickets",
            )
        user_row = (
            await db.execute(
                select(User).where(
                    User.id == body.user_id,
                    User.tenant_id == tenant.id,
                )
            )
        ).scalar_one_or_none()
        if user_row is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="user not found in tenant",
            )
        if (user_row.role or "").lower() not in _MONITOR_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="monitor tickets require manager or admin role",
            )
        user_id = str(user_row.id)
    elif body.user_id is not None:
        # Agent tickets don't require a user, but if one is supplied,
        # bind it so downstream audit has the originator.
        user_id = str(body.user_id)

    session_id = body.session_id or f"s-{uuid.uuid4().hex[:16]}"
    issued = await issue_ticket(
        redis,
        tenant_id=str(tenant.id),
        session_id=session_id,
        role=body.role,
        user_id=user_id,
        ttl_seconds=DEFAULT_TICKET_TTL_SEC,
    )
    return TicketResponse(
        ticket=issued["ticket"],
        session_id=issued["session_id"],
        role=issued["role"],
        expires_at=issued["expires_at"],
    )
