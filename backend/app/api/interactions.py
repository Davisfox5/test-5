"""Interactions API — unified CRUD for voice, email, chat.

SMS and WhatsApp channels are stubbed out — see
``backend/app/services/sms_ingest.py``.  Existing rows with those channel
values remain queryable but new ones cannot be created through the API.
"""

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import ActionItem, Contact, CustomerOutcomeEvent, Interaction, Tenant
from backend.app.services.kb.context_dispatch import schedule_customer_brief_rebuild
from backend.app.services.webhook_dispatcher import emit_event
from backend.app.services.webhook_events import CUSTOMER_OUTCOME_EVENT_MAP

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


# SMS / WhatsApp intentionally excluded from the accepted channel list.
# They confound email/voice analysis during early tenant onboarding and
# are re-enabled by adding them back to this Literal.
class InteractionCreate(BaseModel):
    channel: Literal["voice", "email", "chat"]
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
    channel: Optional[str] = Query(None, description="Filter by channel: voice|email|chat"),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Free-text search across title / raw_text / caller_phone"),
    date_from: Optional[datetime] = Query(None, description="Inclusive lower bound on created_at"),
    date_to: Optional[datetime] = Query(None, description="Inclusive upper bound on created_at"),
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
    if q:
        # ILIKE across the columns the SPA list view searches over.
        needle = f"%{q}%"
        stmt = stmt.where(
            or_(
                Interaction.title.ilike(needle),
                Interaction.raw_text.ilike(needle),
                Interaction.caller_phone.ilike(needle),
            )
        )
    if date_from is not None:
        stmt = stmt.where(Interaction.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Interaction.created_at <= date_to)
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


# Imported lazily so the schema lives next to the action-items module
# while the route shape (nested under /interactions/{id}) belongs here —
# the SPA detail page expects to load both halves from the same shape.
from backend.app.api.action_items import ActionItemOut  # noqa: E402


@router.get(
    "/interactions/{interaction_id}/action-items",
    response_model=List[ActionItemOut],
)
async def list_interaction_action_items(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Action items attached to a specific interaction.

    Tenant-scoped: a 404 on a stranger's interaction id, an empty list
    when the interaction exists but has no items.
    """
    interaction_stmt = select(Interaction.id).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    if (await db.execute(interaction_stmt)).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Interaction not found")

    items_stmt = (
        select(ActionItem)
        .where(
            ActionItem.tenant_id == tenant.id,
            ActionItem.interaction_id == interaction_id,
        )
        .order_by(ActionItem.created_at.asc())
    )
    rows = (await db.execute(items_stmt)).scalars().all()
    return list(rows)


@router.post("/interactions", response_model=InteractionOut, status_code=201)
async def create_interaction(
    body: InteractionCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Create a text-based interaction (email, chat).

    For voice uploads, use POST /interactions/upload instead.  Email
    ingestion normally runs through the OAuth poller, not this endpoint.
    SMS/WhatsApp are disabled (see services/sms_ingest.py).
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

    # Dispatch the text-analysis pipeline (email/chat share the same branch).
    try:
        from backend.app.tasks import process_text_interaction

        process_text_interaction.delay(str(interaction.id))
    except Exception:  # pragma: no cover — Celery may be unavailable in tests
        pass

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

    # Read bytes into memory so we can stage them in S3. Staging is
    # short-lived — the Celery voice task deletes the object after the
    # transcript lands.
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


# ── External recording ingest ────────────────────────────


class IngestRecordingIn(BaseModel):
    """Payload for JSON-based recording ingest.

    External recording systems (MiaRec, Dubber, Teams, MetaSwitch, etc.)
    post either a pre-signed ``audio_url`` we can fetch or nothing at
    all — in which case ``POST /interactions/ingest-recording`` should
    be called as ``multipart/form-data`` with a file part named
    ``file`` instead of JSON.
    """

    audio_url: str = Field(..., description="HTTPS URL to the audio file")
    title: Optional[str] = None
    caller_phone: Optional[str] = None
    agent_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    direction: Optional[Literal["inbound", "outbound", "internal"]] = None
    source: Optional[str] = Field(
        default=None,
        description="Provider slug, e.g. 'miarec', 'dubber', 'teams', 'metaswitch'",
    )
    external_call_id: Optional[str] = Field(
        default=None,
        description="Provider's own call id — mirrored into interaction.thread_id for traceability",
    )
    duration_seconds: Optional[int] = None
    started_at: Optional[datetime] = None
    engine: Literal["deepgram", "whisper"] = "deepgram"


@router.post("/interactions/ingest-recording", response_model=InteractionOut, status_code=201)
async def ingest_recording(
    payload: Optional[IngestRecordingIn] = None,
    file: Optional[UploadFile] = File(default=None),
    audio_url: Optional[str] = Form(default=None),
    title: Optional[str] = Form(default=None),
    caller_phone: Optional[str] = Form(default=None),
    agent_id: Optional[uuid.UUID] = Form(default=None),
    contact_id: Optional[uuid.UUID] = Form(default=None),
    direction: Optional[str] = Form(default=None),
    source: Optional[str] = Form(default=None),
    external_call_id: Optional[str] = Form(default=None),
    duration_seconds: Optional[int] = Form(default=None),
    engine: str = Form(default="deepgram"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Ingest a post-call recording from an external recording system.

    Two shapes:

    * ``POST … Content-Type: application/json`` with ``{audio_url, …}``
      — we hand the URL to Deepgram directly, no bytes touch our disk.
    * ``POST … Content-Type: multipart/form-data`` with ``file`` (the
      audio) + optional metadata fields — we stage the bytes in S3
      briefly, the worker transcribes then deletes.

    The audio itself is discarded after transcription either way;
    metadata (``external_call_id``, ``direction``, ``source``, participant
    ids) is persisted on the Interaction row.
    """
    # Collapse the two entry shapes into one set of locals.
    if payload is not None:
        resolved_url = payload.audio_url
        resolved_title = payload.title
        resolved_caller = payload.caller_phone
        resolved_agent = payload.agent_id
        resolved_contact = payload.contact_id
        resolved_direction = payload.direction
        resolved_source = payload.source
        resolved_external = payload.external_call_id
        resolved_duration = payload.duration_seconds
        resolved_engine = payload.engine
    else:
        resolved_url = audio_url
        resolved_title = title
        resolved_caller = caller_phone
        resolved_agent = agent_id
        resolved_contact = contact_id
        resolved_direction = direction
        resolved_source = source
        resolved_external = external_call_id
        resolved_duration = duration_seconds
        resolved_engine = engine

    if resolved_engine not in ("deepgram", "whisper"):
        raise HTTPException(status_code=400, detail="engine must be deepgram|whisper")
    if not resolved_url and file is None:
        raise HTTPException(
            status_code=400,
            detail="Provide either audio_url (JSON) or a multipart file upload",
        )

    interaction = Interaction(
        tenant_id=tenant.id,
        channel="voice",
        source=resolved_source or "external-recording",
        direction=resolved_direction,
        title=resolved_title or (resolved_external or "Ingested recording"),
        caller_phone=resolved_caller,
        agent_id=resolved_agent,
        engine=resolved_engine,
        status="processing",
        duration_seconds=resolved_duration,
        thread_id=resolved_external,
    )
    db.add(interaction)
    await db.flush()

    # URL mode: store the pointer, Celery passes it to Deepgram directly.
    if resolved_url:
        interaction.audio_url = resolved_url
    else:
        # Multipart mode: stage bytes into S3; worker cleans up.
        import asyncio as _asyncio
        from backend.app.services import s3_audio

        audio_bytes = await file.read()
        if len(audio_bytes) > 500 * 1024 * 1024:
            raise HTTPException(400, "File too large (max 500MB)")
        content_type = file.content_type or "audio/wav"
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
            interaction.status = "failed"
            interaction.insights = {"error": "audio_storage_not_configured"}
            return interaction
        except Exception as exc:
            interaction.status = "failed"
            interaction.insights = {"error": f"upload_failed: {exc}"[:500]}
            return interaction

    # Dispatch the same voice pipeline used for the manual upload path.
    try:
        from backend.app.tasks import process_voice_interaction

        process_voice_interaction.delay(str(interaction.id))
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "Celery dispatch failed for ingested interaction %s", interaction.id,
            exc_info=True,
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
