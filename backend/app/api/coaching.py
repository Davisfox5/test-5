"""Coaching sessions API.

Exposes a manager+ list of recent live-coaching sessions for the tenant
so the SPA's ``/coaching`` "Recent sessions" panel has something to
render on first load. Each row joins the ``LiveSession`` to the agent's
display name and (when available) the analyzed ``Interaction`` so the
manager can click straight through to the call.

The live WebSocket itself, ticket exchange, and per-session events stay
in :mod:`backend.app.api.websocket` and :mod:`backend.app.api.ws_tickets`
— this module is REST-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, require_role
from backend.app.db import get_db
from backend.app.models import Interaction, LiveSession, User

router = APIRouter()


class CoachingSessionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: Optional[str] = None
    interaction_id: Optional[uuid.UUID] = None
    interaction_title: Optional[str] = None
    source: Optional[str] = None
    status: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None

    model_config = {"from_attributes": True}


class CoachingSessionList(BaseModel):
    items: List[CoachingSessionOut]
    total: int


@router.get("/coaching/sessions", response_model=CoachingSessionList)
async def list_coaching_sessions(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("manager")),
) -> CoachingSessionList:
    """Recent live-coaching sessions for the tenant.

    Manager+ scope — agents see their own calls in /interactions, but
    the coaching room view is a managerial workflow. Newest first.
    Tenant-scoped against ``principal.tenant.id`` so a manager in
    tenant A never sees rows from tenant B even if they guess an id.
    """
    tenant_id = principal.tenant.id

    # Pagination footer uses a real SQL COUNT(*) (was ``len(... .all())``
    # which materialised every primary key into Python — a ~500ms tax on
    # large tenants even when the user is paging the first page of 50K
    # rows). One COUNT + one LIMIT+OFFSET; the heavy lifting now stays
    # in the database.
    total_stmt = (
        select(func.count(LiveSession.id)).where(LiveSession.tenant_id == tenant_id)
    )
    total = int((await db.execute(total_stmt)).scalar_one())

    stmt = (
        select(LiveSession, User, Interaction)
        .join(User, User.id == LiveSession.agent_id, isouter=True)
        .join(
            Interaction,
            Interaction.id == LiveSession.interaction_id,
            isouter=True,
        )
        .where(LiveSession.tenant_id == tenant_id)
        .order_by(LiveSession.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()

    items: List[CoachingSessionOut] = []
    for sess, agent_user, interaction in rows:
        # ``ended_at`` is ``None`` for active sessions; show "live" duration
        # as the gap from start → now so the row isn't blank mid-call.
        duration: Optional[int] = None
        if sess.ended_at is not None:
            try:
                duration = int(
                    (sess.ended_at - sess.started_at).total_seconds()
                )
            except (TypeError, AttributeError):
                duration = None

        agent_name: Optional[str] = None
        if agent_user is not None:
            agent_name = agent_user.name or (
                agent_user.email.split("@", 1)[0] if agent_user.email else None
            )

        interaction_title: Optional[str] = None
        if interaction is not None:
            interaction_title = interaction.title

        items.append(
            CoachingSessionOut(
                id=sess.id,
                tenant_id=sess.tenant_id,
                agent_id=sess.agent_id,
                agent_name=agent_name,
                interaction_id=sess.interaction_id,
                interaction_title=interaction_title,
                source=sess.source,
                status=sess.status,
                started_at=sess.started_at,
                ended_at=sess.ended_at,
                duration_seconds=duration,
            )
        )

    return CoachingSessionList(items=items, total=total)
