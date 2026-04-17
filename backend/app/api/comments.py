"""Comments API — time-stamped comments on interactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.models import Tenant
from backend.app.db import get_db
from backend.app.models import Interaction, InteractionComment

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class CommentCreate(BaseModel):
    timestamp_sec: Optional[float] = None
    body: str


class CommentOut(BaseModel):
    id: uuid.UUID
    interaction_id: uuid.UUID
    user_id: uuid.UUID
    timestamp_sec: Optional[float]
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Placeholder user dependency ──────────────────────────

DEMO_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def get_current_user_id() -> uuid.UUID:
    """Placeholder — returns demo user until auth middleware is built."""
    return DEMO_USER_ID


# ── Helpers ──────────────────────────────────────────────


async def _verify_interaction(
    interaction_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """Raise 404 if the interaction does not belong to the tenant."""
    stmt = select(Interaction.id).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Interaction not found")


# ── Endpoints ────────────────────────────────────────────


@router.get(
    "/interactions/{interaction_id}/comments",
    response_model=List[CommentOut],
)
async def list_comments(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> List[CommentOut]:
    """List all comments for an interaction, ordered by creation time."""
    await _verify_interaction(interaction_id, tenant.id, db)

    stmt = (
        select(InteractionComment)
        .where(
            InteractionComment.interaction_id == interaction_id,
            InteractionComment.tenant_id == tenant.id,
        )
        .order_by(InteractionComment.created_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post(
    "/interactions/{interaction_id}/comments",
    response_model=CommentOut,
    status_code=201,
)
async def create_comment(
    interaction_id: uuid.UUID,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    user_id: uuid.UUID = Depends(get_current_user_id),
) -> CommentOut:
    """Create a new comment on an interaction."""
    await _verify_interaction(interaction_id, tenant.id, db)

    comment = InteractionComment(
        interaction_id=interaction_id,
        tenant_id=tenant.id,
        user_id=user_id,
        timestamp_sec=body.timestamp_sec,
        body=body.body,
    )
    db.add(comment)
    await db.flush()
    return comment


@router.delete(
    "/interactions/{interaction_id}/comments/{comment_id}",
    status_code=204,
)
async def delete_comment(
    interaction_id: uuid.UUID,
    comment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> None:
    """Delete a comment by ID."""
    stmt = select(InteractionComment).where(
        InteractionComment.id == comment_id,
        InteractionComment.interaction_id == interaction_id,
        InteractionComment.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")

    await db.delete(comment)
