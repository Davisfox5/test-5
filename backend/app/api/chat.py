"""Ask Linda chat API — SSE streaming + write-proposal confirm/cancel."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_user_or_tenant
from backend.app.db import get_db
from backend.app.models import ActionItem, Tenant, User, WriteProposal
from backend.app.services.linda_agent import (
    AgentContext,
    get_or_create_conversation,
    run_chat_turn,
)
from backend.app.services.rate_limiter import LindaRateLimiter

logger = logging.getLogger(__name__)
router = APIRouter()

_rate_limiter = LindaRateLimiter()


def _require_not_white_label(tenant: Tenant) -> None:
    if getattr(tenant, "is_white_label", False):
        # White-label tenants get 404 — the feature is invisible, not "forbidden".
        raise HTTPException(status_code=404, detail="Not found")


async def _resolve_current_user(
    request: Request, db: AsyncSession, tenant: Tenant
) -> Optional[User]:
    """Best-effort resolve the Clerk-authenticated user for logging on messages."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token.startswith("clerk_"):
        return None
    stmt = select(User).where(User.clerk_user_id == token, User.tenant_id == tenant.id)
    return (await db.execute(stmt)).scalar_one_or_none()


# ── Schemas ────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: Optional[uuid.UUID] = None


class ProposalOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    kind: str
    payload: dict
    status: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: Optional[datetime] = None
    resulting_entity_id: Optional[uuid.UUID] = None

    model_config = {"from_attributes": True}


# ── SSE helper ─────────────────────────────────────────────────────────────


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


async def _stream_chat(
    ctx: AgentContext, user_message: str, conversation_id: uuid.UUID
) -> AsyncIterator[str]:
    yield _sse({"type": "conversation", "conversation_id": str(conversation_id)})
    try:
        async for event in run_chat_turn(ctx, user_message):
            yield _sse(event)
        await ctx.db.commit()
    except Exception as exc:
        logger.exception("chat stream failed")
        await ctx.db.rollback()
        yield _sse({"type": "error", "message": str(exc)})


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/chat/ping")
async def chat_ping(
    tenant: Tenant = Depends(get_current_user_or_tenant),
) -> Dict[str, bool]:
    """Lightweight liveness check — 404 if the tenant is white-label, 200 otherwise."""
    _require_not_white_label(tenant)
    return {"ok": True}


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    tenant: Tenant = Depends(get_current_user_or_tenant),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Ask Linda. Streams SSE events: text deltas, tool_use, tool_result, proposal, done."""
    _require_not_white_label(tenant)

    rate = await _rate_limiter.check(str(tenant.id))
    if not rate.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(rate.retry_after_s)},
        )

    user = await _resolve_current_user(request, db, tenant)
    conversation = await get_or_create_conversation(
        db, tenant, user, payload.conversation_id
    )
    ctx = AgentContext(db=db, tenant=tenant, user=user, conversation_id=conversation.id)

    return StreamingResponse(
        _stream_chat(ctx, payload.message, conversation.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for real-time flush
        },
    )


@router.post("/chat/proposals/{proposal_id}/confirm", response_model=ProposalOut)
async def confirm_proposal(
    proposal_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_user_or_tenant),
    db: AsyncSession = Depends(get_db),
) -> ProposalOut:
    _require_not_white_label(tenant)

    proposal = (
        await db.execute(
            select(WriteProposal).where(
                WriteProposal.id == proposal_id, WriteProposal.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal is {proposal.status}")

    if proposal.expires_at < datetime.now(timezone.utc):
        proposal.status = "expired"
        await db.commit()
        raise HTTPException(status_code=410, detail="Proposal has expired")

    resulting_id = await _execute_proposal(db, proposal, tenant)
    proposal.status = "confirmed"
    proposal.confirmed_at = datetime.now(timezone.utc)
    proposal.resulting_entity_id = resulting_id
    await db.commit()
    await db.refresh(proposal)
    return proposal


@router.post("/chat/proposals/{proposal_id}/cancel", response_model=ProposalOut)
async def cancel_proposal(
    proposal_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_user_or_tenant),
    db: AsyncSession = Depends(get_db),
) -> ProposalOut:
    _require_not_white_label(tenant)

    proposal = (
        await db.execute(
            select(WriteProposal).where(
                WriteProposal.id == proposal_id, WriteProposal.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal is {proposal.status}")

    proposal.status = "cancelled"
    await db.commit()
    await db.refresh(proposal)
    return proposal


# ── Proposal executor ──────────────────────────────────────────────────────


async def _execute_proposal(
    db: AsyncSession, proposal: WriteProposal, tenant: Tenant
) -> Optional[uuid.UUID]:
    """Dispatch a confirmed proposal to the real mutator. Returns the created entity id."""
    payload = proposal.payload or {}

    if proposal.kind == "action_item":
        interaction_id = payload.get("interaction_id")
        try:
            interaction_uuid = uuid.UUID(interaction_id) if interaction_id else None
        except ValueError:
            interaction_uuid = None
        if interaction_uuid is None:
            raise HTTPException(
                status_code=422,
                detail="action_item proposal requires an interaction_id",
            )
        assignee_id: Optional[uuid.UUID] = None
        if payload.get("assignee_email"):
            assignee_id = (
                await db.execute(
                    select(User.id).where(
                        User.tenant_id == tenant.id,
                        User.email == payload["assignee_email"],
                    )
                )
            ).scalar_one_or_none()
        due: Optional[date] = None
        if payload.get("due_date"):
            try:
                due = date.fromisoformat(payload["due_date"])
            except ValueError:
                due = None

        item = ActionItem(
            interaction_id=interaction_uuid,
            tenant_id=tenant.id,
            assigned_to=assignee_id,
            title=payload["title"],
            description=payload.get("description"),
            priority=payload.get("priority", "medium"),
            due_date=due,
            status="pending",
        )
        db.add(item)
        await db.flush()
        return item.id

    if proposal.kind == "email_draft":
        # Email send integration is out of scope for this commit — we stage the
        # draft on the related ActionItem's email_draft field so the UI can
        # surface it for send-from-Gmail on the action item page.
        interaction_id = payload.get("interaction_id")
        try:
            interaction_uuid = uuid.UUID(interaction_id) if interaction_id else None
        except ValueError:
            interaction_uuid = None
        if interaction_uuid is None:
            raise HTTPException(
                status_code=422,
                detail="email_draft proposal requires a valid interaction_id",
            )
        item = ActionItem(
            interaction_id=interaction_uuid,
            tenant_id=tenant.id,
            title=payload.get("subject") or "Follow-up email",
            description="Email draft proposed by Linda.",
            priority="medium",
            status="pending",
            email_draft={
                "subject": payload.get("subject"),
                "body": payload.get("body"),
                "recipients": payload.get("recipients", []),
            },
        )
        db.add(item)
        await db.flush()
        return item.id

    if proposal.kind == "crm_update":
        # CRM push is wired via the Integration service; executing here would
        # require per-tenant credentials. Record the confirmed payload on the
        # proposal and return — the scheduler/Celery worker can pick it up.
        return None

    raise HTTPException(status_code=422, detail=f"unknown proposal kind: {proposal.kind}")
