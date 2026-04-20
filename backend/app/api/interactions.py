"""Interactions API — unified CRUD for voice, SMS, email, chat, WhatsApp."""

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Contact, CustomerOutcomeEvent, Interaction, Tenant
from backend.app.services.kb.context_dispatch import schedule_customer_brief_rebuild
from backend.app.services.webhook_dispatcher import emit_event
from backend.app.services.webhook_events import CUSTOMER_OUTCOME_EVENT_MAP

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class InteractionCreate(BaseModel):
    channel: Literal["voice", "sms", "email", "chat", "whatsapp"]
    source: Optional[str] = None
    direction: Optional[Literal["inbound", "outbound", "internal"]] = None
    title: Optional[str] = None
    raw_text: Optional[str] = None
    thread_id: Optional[str] = None
    caller_phone: Optional[str] = None
    agent_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    participants: Optional[List[dict]] = None


class InteractionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    channel: str
    source: Optional[str]
    direction: Optional[str]
    title: Optional[str]
    status: str
    duration_seconds: Optional[int]
    caller_phone: Optional[str]
    complexity_score: Optional[float]
    analysis_tier: Optional[str]
    call_metrics: dict
    insights: dict
    pii_redacted: bool
    detected_language: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class InteractionDetail(InteractionOut):
    transcript: list
    transcript_translated: Optional[list]
    raw_text: Optional[str]
    thread_id: Optional[str]
    participants: list
    agent_id: Optional[uuid.UUID]
    contact_id: Optional[uuid.UUID]


class InteractionUpdate(BaseModel):
    title: Optional[str] = None
    contact_id: Optional[uuid.UUID] = None


# ── Endpoints ────────────────────────────────────────────


@router.get("/interactions", response_model=List[InteractionOut])
async def list_interactions(
    channel: Optional[str] = Query(None, description="Filter by channel: voice|sms|email|chat|whatsapp"),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(Interaction)
        .where(Interaction.tenant_id == tenant.id)
        .order_by(Interaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if channel:
        stmt = stmt.where(Interaction.channel == channel)
    if status:
        stmt = stmt.where(Interaction.status == status)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/interactions/{interaction_id}", response_model=InteractionDetail)
async def get_interaction(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Interaction).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    interaction = result.scalar_one_or_none()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")
    return interaction


@router.post("/interactions", response_model=InteractionOut, status_code=201)
async def create_interaction(
    body: InteractionCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Create a text-based interaction (SMS, email, chat, WhatsApp text).
    For voice uploads, use POST /interactions/upload instead.
    """
    interaction = Interaction(
        tenant_id=tenant.id,
        channel=body.channel,
        source=body.source or "api",
        direction=body.direction,
        title=body.title,
        raw_text=body.raw_text,
        thread_id=body.thread_id,
        caller_phone=body.caller_phone,
        agent_id=body.agent_id,
        contact_id=body.contact_id,
        participants=body.participants or [],
        status="processing",
    )
    db.add(interaction)
    await db.flush()

    # TODO: dispatch Celery task for text analysis pipeline
    # analyze_text_interaction.delay(str(interaction.id))

    return interaction


@router.post("/interactions/upload", response_model=InteractionOut, status_code=201)
async def upload_voice_interaction(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    engine: Literal["deepgram", "whisper"] = Form("deepgram"),
    caller_phone: Optional[str] = Form(None),
    agent_id: Optional[uuid.UUID] = Form(None),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Upload a voice recording for transcription and analysis."""
    allowed_types = {
        "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
        "audio/ogg", "audio/webm", "video/webm",
    }
    if file.content_type and file.content_type not in allowed_types:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}")

    # Read file content (in production, stream to S3 if tenant.audio_storage_enabled)
    audio_bytes = await file.read()
    if len(audio_bytes) > 500 * 1024 * 1024:  # 500MB
        raise HTTPException(400, "File too large (max 500MB)")

    interaction = Interaction(
        tenant_id=tenant.id,
        channel="voice",
        source="upload",
        direction="inbound",
        title=title or file.filename,
        caller_phone=caller_phone,
        agent_id=agent_id,
        engine=engine,
        status="processing",
    )
    db.add(interaction)
    await db.flush()

    # Stash the audio in S3 under a tenant-scoped key so it can be
    # streamed into the Deepgram/Whisper transcription worker without
    # holding the bytes in memory through the Celery round-trip. Key
    # layout mirrors the recording archive:
    #   uploads/{tenant_id}/{interaction_id}.{ext}
    import asyncio as _asyncio
    from backend.app.services import s3_audio

    content_type = file.content_type or "audio/wav"
    upload_key = f"uploads/{tenant.id}/{interaction.id}.{s3_audio._content_type_extension(content_type)}"

    try:
        stored = await _asyncio.to_thread(
            s3_audio.upload_bytes,
            tenant_id=tenant.id,
            recording_id=interaction.id,
            data=audio_bytes,
            content_type=content_type,
        )
        interaction.audio_s3_key = stored.s3_key
    except s3_audio.S3NotConfigured:
        # Without S3 we can't run the batch pipeline for uploads — mark
        # the row so admins see the gap rather than a silent never-
        # processed interaction.
        interaction.status = "failed"
        interaction.insights = {"error": "audio_storage_not_configured"}
        return interaction
    except Exception as exc:
        interaction.status = "failed"
        interaction.insights = {"error": f"upload_failed: {exc}"[:500]}
        return interaction

    # Dispatch the batch pipeline. ``process_voice_interaction`` already
    # handles both the "transcript already populated" path (live calls)
    # and the "audio_s3_key present" path we're setting up here.
    try:
        from backend.app.tasks import process_voice_interaction

        process_voice_interaction.delay(str(interaction.id))
    except Exception:
        # Celery not available (local dev, tests) — the row is saved and
        # the admin can trigger analysis manually. Don't fail the upload.
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "Celery dispatch failed for uploaded interaction %s", interaction.id, exc_info=True,
        )

    return interaction


@router.patch("/interactions/{interaction_id}", response_model=InteractionOut)
async def update_interaction(
    interaction_id: uuid.UUID,
    body: InteractionUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Interaction).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    interaction = result.scalar_one_or_none()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")

    if body.title is not None:
        interaction.title = body.title
    if body.contact_id is not None:
        interaction.contact_id = body.contact_id

    return interaction


@router.delete("/interactions/{interaction_id}", status_code=204)
async def delete_interaction(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Interaction).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    interaction = result.scalar_one_or_none()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")

    await db.delete(interaction)


# ── Outcome logging ─────────────────────────────────────────────────


class OutcomeIn(BaseModel):
    outcome_type: str = Field(
        ...,
        description=(
            "One of: booked_meeting, qualified, disqualified, demo_scheduled, "
            "proposal_sent, closed_won, closed_lost, resolved, escalated, "
            "unresolved, refund_processed, follow_up_scheduled, info_shared, "
            "no_decision, upsell_opportunity."
        ),
    )
    outcome_value: Optional[float] = Field(
        None, description="Dollars for sales, CSAT delta for support, metric for other."
    )
    outcome_notes: Optional[str] = None
    # Optional customer-level event to record alongside the call disposition
    # (e.g., closed_won → also record 'became_customer' or 'upsold').
    customer_event_type: Optional[
        Literal[
            "became_customer",
            "upsold",
            "renewed",
            "churned",
            "satisfaction_change",
            "escalation",
            "advocate_signal",
            "at_risk_flagged",
        ]
    ] = None
    customer_event_magnitude: Optional[float] = None


@router.post("/interactions/{interaction_id}/outcome")
async def log_outcome(
    interaction_id: uuid.UUID,
    body: OutcomeIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Agent-logged disposition for a call.

    Overwrites any AI-inferred outcome on the Interaction row and, when
    ``customer_event_type`` is provided, records a ``CustomerOutcomeEvent``
    for the associated customer so the agents can learn from it.
    """
    stmt = select(Interaction).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    interaction = (await db.execute(stmt)).scalar_one_or_none()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")

    interaction.outcome_type = body.outcome_type
    interaction.outcome_value = body.outcome_value
    interaction.outcome_confidence = 1.0  # agent-confirmed
    interaction.outcome_source = "agent_logged"
    interaction.outcome_notes = body.outcome_notes
    interaction.outcome_captured_at = datetime.utcnow()

    event_created: Optional[str] = None
    customer_id_for_rebuild: Optional[uuid.UUID] = None
    if interaction.contact_id:
        contact = await db.get(Contact, interaction.contact_id)
        if contact and contact.customer_id:
            customer_id_for_rebuild = contact.customer_id
            if body.customer_event_type:
                ev = CustomerOutcomeEvent(
                    tenant_id=tenant.id,
                    customer_id=contact.customer_id,
                    interaction_id=interaction.id,
                    event_type=body.customer_event_type,
                    magnitude=body.customer_event_magnitude,
                    signal_strength=1.0,
                    reason=body.outcome_notes,
                    source="agent_logged",
                )
                db.add(ev)
                await db.flush()
                event_created = str(ev.id)

                # Fan out the lifecycle event — receivers subscribed to
                # e.g. ``customer.churned`` get notified here too.
                wh_event = CUSTOMER_OUTCOME_EVENT_MAP.get(body.customer_event_type)
                if wh_event:
                    await emit_event(
                        db,
                        tenant.id,
                        wh_event,
                        {
                            "customer_id": str(contact.customer_id),
                            "interaction_id": str(interaction.id),
                            "event_type": body.customer_event_type,
                            "magnitude": body.customer_event_magnitude,
                            "signal_strength": 1.0,
                            "reason": body.outcome_notes,
                            "source": "agent_logged",
                        },
                    )

    # Always emit outcome_inferred for the explicit disposition, regardless
    # of whether a lifecycle event was attached.
    await emit_event(
        db,
        tenant.id,
        "interaction.outcome_inferred",
        {
            "interaction_id": str(interaction_id),
            "outcome_type": interaction.outcome_type,
            "outcome_value": interaction.outcome_value,
            "outcome_confidence": interaction.outcome_confidence,
            "outcome_source": "agent_logged",
            "outcome_notes": body.outcome_notes,
        },
    )

    if customer_id_for_rebuild is not None:
        await schedule_customer_brief_rebuild(tenant.id, customer_id_for_rebuild)

    return {
        "interaction_id": str(interaction_id),
        "outcome_type": interaction.outcome_type,
        "customer_event_id": event_created,
    }
