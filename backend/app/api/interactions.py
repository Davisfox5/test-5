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
from backend.app.models import Interaction, Tenant

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

    # TODO: Save audio to temp storage or S3, then dispatch Celery task
    # transcribe_and_analyze.delay(str(interaction.id), audio_path, engine)

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
