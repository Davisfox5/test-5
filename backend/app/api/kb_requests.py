"""KB-article-request endpoints.

The ``update_kb_article`` and ``escalate_recurring_issue`` Apply paths
in the manager portal now write to ``kb_article_requests`` (this
table); previously they wrote to ``CoachingNote`` which mixed KB edits
into the rep coaching queue. This API surfaces the dedicated KB-owner
inbox plus the publish/dismiss controls.

Gated on Support-motion access (``it_support`` in either ``agent_domains``
or ``manager_domains``) plus tenant-admin override. CS managers can
file requests too — Support is the assignee, but a CS team often
spots the gap.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
)
from backend.app.db import get_db
from backend.app.models import KBArticleRequest, User

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic shapes ────────────────────────────────────────────────────


class KBArticleRequestOut(BaseModel):
    id: uuid.UUID
    topic: str
    rationale: Optional[str]
    proposed_body: Optional[str]
    status: str
    priority: str
    requested_by_user_id: Optional[uuid.UUID]
    assigned_to: Optional[uuid.UUID]
    assigned_to_name: Optional[str]
    source_recommendation_id: Optional[uuid.UUID]
    source_kb_chunk_id: Optional[uuid.UUID]
    created_at: datetime
    published_at: Optional[datetime]
    dismissed_at: Optional[datetime]


class KBArticleRequestCreateIn(BaseModel):
    topic: str = Field(..., min_length=1, max_length=300)
    rationale: Optional[str] = None
    proposed_body: Optional[str] = None
    priority: Literal["high", "medium", "low"] = "medium"
    source_kb_chunk_id: Optional[uuid.UUID] = None


class KBArticleRequestPatchIn(BaseModel):
    status: Optional[Literal["open", "in_progress", "published", "dismissed"]] = None
    priority: Optional[Literal["high", "medium", "low"]] = None
    assigned_to: Optional[uuid.UUID] = None
    proposed_body: Optional[str] = None
    dismiss_reason: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────


def _can_access_kb(principal: AuthPrincipal) -> bool:
    if principal.is_tenant_admin:
        return True
    if principal.source == "api_key":
        return True
    if "it_support" in principal.agent_domains:
        return True
    if "it_support" in principal.manager_domains:
        return True
    # CS managers can FILE requests but the API uses the same gate; the
    # POST handler doesn't enforce a stricter check today.
    if "customer_service" in principal.manager_domains:
        return True
    return False


async def _kb_gate(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> AuthPrincipal:
    if not _can_access_kb(principal):
        raise HTTPException(
            status_code=403,
            detail="Requires IT Support or CS Manager access.",
        )
    return principal


async def _load(
    db: AsyncSession, tenant_id: uuid.UUID, req_id: uuid.UUID
) -> KBArticleRequest:
    r = await db.get(KBArticleRequest, req_id)
    if r is None or r.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Request not found")
    return r


async def _to_out(db: AsyncSession, r: KBArticleRequest) -> KBArticleRequestOut:
    assigned_to_name: Optional[str] = None
    if r.assigned_to is not None:
        u = await db.get(User, r.assigned_to)
        if u is not None:
            assigned_to_name = u.name or u.email
    return KBArticleRequestOut(
        id=r.id,
        topic=r.topic,
        rationale=r.rationale,
        proposed_body=r.proposed_body,
        status=r.status,
        priority=r.priority,
        requested_by_user_id=r.requested_by_user_id,
        assigned_to=r.assigned_to,
        assigned_to_name=assigned_to_name,
        source_recommendation_id=r.source_recommendation_id,
        source_kb_chunk_id=r.source_kb_chunk_id,
        created_at=r.created_at,
        published_at=r.published_at,
        dismissed_at=r.dismissed_at,
    )


# ── Routes ─────────────────────────────────────────────────────────────


@router.get(
    "/kb/requests",
    response_model=List[KBArticleRequestOut],
)
async def list_requests(
    status: Optional[Literal["open", "in_progress", "published", "dismissed"]] = Query(None),
    mine_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_kb_gate),
) -> List[KBArticleRequestOut]:
    stmt = select(KBArticleRequest).where(
        KBArticleRequest.tenant_id == principal.tenant.id
    )
    if status is not None:
        stmt = stmt.where(KBArticleRequest.status == status)
    else:
        stmt = stmt.where(KBArticleRequest.status.in_(("open", "in_progress")))
    if mine_only and principal.user_id is not None:
        stmt = stmt.where(KBArticleRequest.assigned_to == principal.user_id)
    stmt = stmt.order_by(desc(KBArticleRequest.created_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [await _to_out(db, r) for r in rows]


@router.post(
    "/kb/requests",
    response_model=KBArticleRequestOut,
    status_code=201,
)
async def create_request(
    body: KBArticleRequestCreateIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_kb_gate),
) -> KBArticleRequestOut:
    req = KBArticleRequest(
        tenant_id=principal.tenant.id,
        requested_by_user_id=principal.user_id,
        topic=body.topic,
        rationale=body.rationale,
        proposed_body=body.proposed_body,
        priority=body.priority,
        source_kb_chunk_id=body.source_kb_chunk_id,
    )
    db.add(req)
    await db.flush()
    return await _to_out(db, req)


@router.get(
    "/kb/requests/{req_id}",
    response_model=KBArticleRequestOut,
)
async def get_request(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_kb_gate),
) -> KBArticleRequestOut:
    r = await _load(db, principal.tenant.id, req_id)
    return await _to_out(db, r)


@router.patch(
    "/kb/requests/{req_id}",
    response_model=KBArticleRequestOut,
)
async def patch_request(
    req_id: uuid.UUID,
    body: KBArticleRequestPatchIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(_kb_gate),
) -> KBArticleRequestOut:
    r = await _load(db, principal.tenant.id, req_id)
    updates = body.model_dump(exclude_none=True)
    now = datetime.now(timezone.utc)
    if updates.get("status") == "published" and r.published_at is None:
        r.published_at = now
    if updates.get("status") == "dismissed" and r.dismissed_at is None:
        r.dismissed_at = now
    for k, v in updates.items():
        setattr(r, k, v)
    await db.flush()
    return await _to_out(db, r)
