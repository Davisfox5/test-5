"""Call Library API — AI-curated snippets for coaching and training."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.models import Tenant
from backend.app.db import get_db
from backend.app.models import InteractionSnippet

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class SnippetOut(BaseModel):
    id: uuid.UUID
    interaction_id: uuid.UUID
    start_time: float
    end_time: float
    snippet_type: Optional[str]
    quality: Optional[str]
    title: Optional[str]
    description: Optional[str]
    transcript_excerpt: Optional[list] = []
    tags: Optional[list] = []
    in_library: bool
    library_category: Optional[str]
    promoted_by: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}

    @validator("transcript_excerpt", "tags", pre=True, always=True)
    def coerce_to_list(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return []
        return v


class PromoteRequest(BaseModel):
    library_category: str


class PaginatedSnippets(BaseModel):
    items: List[SnippetOut]
    total: int
    limit: int
    offset: int


# ── Placeholder user dependency ──────────────────────────

DEMO_MANAGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def get_current_user_id() -> uuid.UUID:
    """Placeholder — returns demo manager until auth middleware is built."""
    return DEMO_MANAGER_ID


# ── Endpoints ────────────────────────────────────────────


@router.get("/library", response_model=PaginatedSnippets)
async def list_library_snippets(
    snippet_type: Optional[str] = Query(None, description="Filter by snippet type"),
    quality: Optional[str] = Query(None, description="Filter by quality rating"),
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter by"),
    agent_id: Optional[uuid.UUID] = Query(None, description="Filter by agent UUID"),
    library_category: Optional[str] = Query(None, description="Filter by library category"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> PaginatedSnippets:
    """List all library snippets for the tenant with optional filters and pagination."""
    base = select(InteractionSnippet).where(
        InteractionSnippet.tenant_id == tenant.id,
        InteractionSnippet.in_library.is_(True),
    )

    if snippet_type:
        base = base.where(InteractionSnippet.snippet_type == snippet_type)
    if quality:
        base = base.where(InteractionSnippet.quality == quality)
    if library_category:
        base = base.where(InteractionSnippet.library_category == library_category)
    if agent_id:
        # Join through the interaction to filter by agent
        from backend.app.models import Interaction

        base = base.join(Interaction, InteractionSnippet.interaction_id == Interaction.id).where(
            Interaction.agent_id == agent_id,
        )
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        for tag in tag_list:
            base = base.where(InteractionSnippet.tags.op("@>")(f'["{tag}"]'))

    # Count total matching rows
    count_stmt = select(func.count()).select_from(base.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    # Fetch page
    stmt = base.order_by(InteractionSnippet.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    items = result.scalars().all()

    return PaginatedSnippets(items=items, total=total, limit=limit, offset=offset)


@router.get("/library/agent/{agent_id}", response_model=PaginatedSnippets)
async def list_agent_snippets(
    agent_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> PaginatedSnippets:
    """List all library snippets for a specific agent."""
    from backend.app.models import Interaction

    base = (
        select(InteractionSnippet)
        .join(Interaction, InteractionSnippet.interaction_id == Interaction.id)
        .where(
            InteractionSnippet.tenant_id == tenant.id,
            InteractionSnippet.in_library.is_(True),
            Interaction.agent_id == agent_id,
        )
    )

    count_stmt = select(func.count()).select_from(base.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = base.order_by(InteractionSnippet.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    items = result.scalars().all()

    return PaginatedSnippets(items=items, total=total, limit=limit, offset=offset)


@router.post("/library/{snippet_id}/promote", response_model=SnippetOut)
async def promote_snippet(
    snippet_id: uuid.UUID,
    body: PromoteRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    user_id: uuid.UUID = Depends(get_current_user_id),
) -> SnippetOut:
    """Manually promote a snippet to the library."""
    stmt = select(InteractionSnippet).where(
        InteractionSnippet.id == snippet_id,
        InteractionSnippet.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    snippet = result.scalar_one_or_none()
    if snippet is None:
        raise HTTPException(status_code=404, detail="Snippet not found")

    snippet.in_library = True
    snippet.library_category = body.library_category
    snippet.promoted_by = str(user_id)

    return snippet


@router.delete("/library/{snippet_id}/demote", status_code=204)
async def demote_snippet(
    snippet_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> None:
    """Remove a snippet from the library."""
    stmt = select(InteractionSnippet).where(
        InteractionSnippet.id == snippet_id,
        InteractionSnippet.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    snippet = result.scalar_one_or_none()
    if snippet is None:
        raise HTTPException(status_code=404, detail="Snippet not found")

    snippet.in_library = False
