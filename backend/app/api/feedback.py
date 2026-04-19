"""Feedback batch ingestion API + transcript correction recording.

The frontend ``useFeedback()`` hook batches events (every 5s or on page
unload) and POSTs them here.  We never write to the DB on the request path —
events are pushed onto a Redis stream and a Celery worker drains it into
``feedback_events``.  This keeps the user-facing latency at zero and makes the
endpoint trivially horizontally scalable.

Transcript corrections are written synchronously because they're a low
volume / high-signal event AND the WER pipeline depends on the row being
durable before the user navigates away.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    FeedbackEvent,
    Interaction,
    Tenant,
    TranscriptCorrection,
)
from backend.app.services import feedback_service

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────


class FeedbackEventIn(BaseModel):
    surface: str = Field(
        ...,
        description="'analysis' | 'email_classifier' | 'email_reply' | 'live_coaching'",
    )
    event_type: str
    signal_type: str = "explicit"  # 'implicit' | 'explicit'
    interaction_id: Optional[uuid.UUID] = None
    conversation_id: Optional[uuid.UUID] = None
    action_item_id: Optional[uuid.UUID] = None
    user_id: Optional[uuid.UUID] = None
    insight_dimension: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    session_id: Optional[uuid.UUID] = None


class FeedbackBatchRequest(BaseModel):
    events: List[FeedbackEventIn]


class FeedbackBatchResponse(BaseModel):
    enqueued: int


class TranscriptCorrectionIn(BaseModel):
    interaction_id: uuid.UUID
    segment_index: int = Field(..., ge=0)
    original_text: str
    corrected_text: str
    confidence_at_correction: Optional[float] = None
    corrected_by: Optional[uuid.UUID] = None


# ── Endpoints ────────────────────────────────────────────


@router.post("/feedback/batch", response_model=FeedbackBatchResponse)
async def feedback_batch(
    body: FeedbackBatchRequest,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Accept a batch of UI events and push to the Redis feedback stream."""
    if not body.events:
        return FeedbackBatchResponse(enqueued=0)

    enqueued = feedback_service.emit_events(
        {
            "tenant_id": tenant.id,
            "surface": ev.surface,
            "event_type": ev.event_type,
            "signal_type": ev.signal_type,
            "interaction_id": ev.interaction_id,
            "conversation_id": ev.conversation_id,
            "action_item_id": ev.action_item_id,
            "user_id": ev.user_id,
            "insight_dimension": ev.insight_dimension,
            "payload": ev.payload,
            "session_id": ev.session_id,
        }
        for ev in body.events
    )
    return FeedbackBatchResponse(enqueued=enqueued)


@router.post("/feedback/transcript-correction", status_code=201)
async def submit_transcript_correction(
    body: TranscriptCorrectionIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Record a manual transcript edit + emit a feedback event."""
    interaction = (
        await db.execute(
            select(Interaction).where(
                Interaction.id == body.interaction_id,
                Interaction.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if interaction is None:
        raise HTTPException(status_code=404, detail="Interaction not found")

    correction = TranscriptCorrection(
        tenant_id=tenant.id,
        interaction_id=body.interaction_id,
        segment_index=body.segment_index,
        original_text=body.original_text,
        corrected_text=body.corrected_text,
        confidence_at_correction=body.confidence_at_correction,
        corrected_by=body.corrected_by,
    )
    db.add(correction)
    await db.flush()

    # Mirror to the feedback stream so vocabulary discovery and WER computation
    # both have a single source of truth to scan.
    feedback_service.emit_event(
        tenant_id=tenant.id,
        surface="analysis",
        event_type="transcript_corrected",
        signal_type="implicit",
        interaction_id=body.interaction_id,
        user_id=body.corrected_by,
        payload={
            "segment_index": body.segment_index,
            "original_text": body.original_text,
            "corrected_text": body.corrected_text,
            "confidence_at_correction": body.confidence_at_correction,
        },
    )
    return {"id": str(correction.id), "status": "recorded"}


@router.get("/feedback/recent", response_model=List[dict])
async def list_recent_feedback(
    surface: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Operational view — list the most recent feedback events for this tenant."""
    stmt = (
        select(FeedbackEvent)
        .where(FeedbackEvent.tenant_id == tenant.id)
        .order_by(FeedbackEvent.created_at.desc())
        .limit(min(limit, 500))
    )
    if surface:
        stmt = stmt.where(FeedbackEvent.surface == surface)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "surface": r.surface,
            "event_type": r.event_type,
            "signal_type": r.signal_type,
            "interaction_id": str(r.interaction_id) if r.interaction_id else None,
            "conversation_id": str(r.conversation_id) if r.conversation_id else None,
            "action_item_id": str(r.action_item_id) if r.action_item_id else None,
            "insight_dimension": r.insight_dimension,
            "payload": r.payload,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
