"""Conversations + AI reply drafting API.

Endpoints:
- GET  /conversations                    — list for the tenant
- GET  /conversations/{id}               — thread detail with messages
- POST /conversations/{id}/draft-reply   — ask Sonnet for a draft
- POST /conversations/{id}/send-reply    — send via Gmail/Graph + log
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Conversation, Integration, Interaction, Tenant, User
from backend.app.services.email_reply import ReplyDrafter
from backend.app.services.email_send import send_via_gmail, send_via_graph
from backend.app.services.token_crypto import decrypt_token

router = APIRouter()


class ConversationOut(BaseModel):
    id: uuid.UUID
    channel: str
    subject: Optional[str]
    classification: Optional[str]
    status: str
    message_count: int
    last_message_at: Optional[datetime]
    insights: dict

    model_config = {"from_attributes": True}


class ConversationMessage(BaseModel):
    id: uuid.UUID
    direction: Optional[str]
    from_address: Optional[str]
    to_addresses: list
    subject: Optional[str]
    raw_text: Optional[str]
    created_at: datetime
    insights: dict

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: List[ConversationMessage]


class DraftReplyRequest(BaseModel):
    extra_instructions: Optional[str] = None


class DraftReplyResponse(BaseModel):
    subject: str
    body: str
    rationale: str
    citations: list
    requires_human_review: bool


class SendReplyRequest(BaseModel):
    subject: str
    body: str
    to: List[EmailStr]
    cc: List[EmailStr] = []
    integration_id: uuid.UUID


# ── Listing / detail ─────────────────────────────────────


@router.get("/conversations", response_model=List[ConversationOut])
async def list_conversations(
    classification: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(Conversation)
        .where(Conversation.tenant_id == tenant.id)
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(min(limit, 200))
    )
    if classification:
        stmt = stmt.where(Conversation.classification == classification)
    if status:
        stmt = stmt.where(Conversation.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.get("/conversations/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conv_id, Conversation.tenant_id == tenant.id
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = (await db.execute(
        select(Interaction)
        .where(Interaction.conversation_id == conv_id)
        .order_by(Interaction.created_at.asc())
    )).scalars().all()

    return ConversationDetail(
        **ConversationOut.model_validate(conv).model_dump(),
        messages=[ConversationMessage.model_validate(m) for m in messages],
    )


# ── Draft + send ─────────────────────────────────────────


@router.post("/conversations/{conv_id}/draft-reply", response_model=DraftReplyResponse)
async def draft_reply(
    conv_id: uuid.UUID,
    body: DraftReplyRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conv_id, Conversation.tenant_id == tenant.id
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    drafter = ReplyDrafter()
    draft = await drafter.draft(db, tenant, conv, body.extra_instructions)
    return DraftReplyResponse(**draft.__dict__)


@router.post("/conversations/{conv_id}/send-reply", status_code=201)
async def send_reply(
    conv_id: uuid.UUID,
    body: SendReplyRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conv_id, Conversation.tenant_id == tenant.id
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    integration = (await db.execute(
        select(Integration).where(
            Integration.id == body.integration_id,
            Integration.tenant_id == tenant.id,
        )
    )).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found")

    # Recover the sender email via the linked User.
    user = (await db.execute(
        select(User).where(User.id == integration.user_id)
    )).scalar_one_or_none() if integration.user_id else None
    from_address = user.email if user else ""

    # Build header threading from the last inbound message on the conversation.
    last_inbound = (await db.execute(
        select(Interaction)
        .where(
            Interaction.conversation_id == conv_id,
            Interaction.direction == "inbound",
        )
        .order_by(Interaction.created_at.desc())
    )).scalars().first()
    in_reply_to = last_inbound.message_id if last_inbound else None
    references = (
        list(last_inbound.references or []) + ([last_inbound.message_id] if last_inbound and last_inbound.message_id else [])
        if last_inbound else []
    )

    # Refresh + decrypt token.
    from backend.app.api.oauth import get_provider_token

    access_token = await get_provider_token(db, integration)

    if integration.provider == "google":
        result = send_via_gmail(
            access_token=access_token,
            from_address=from_address,
            to=list(body.to),
            cc=list(body.cc),
            subject=body.subject,
            body=body.body,
            in_reply_to=in_reply_to,
            references=references,
        )
    elif integration.provider == "microsoft":
        result = send_via_graph(
            access_token=access_token,
            from_address=from_address,
            to=list(body.to),
            cc=list(body.cc),
            subject=body.subject,
            body=body.body,
            in_reply_to=in_reply_to,
            references=references,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {integration.provider}")

    # Persist outbound Interaction (classification stays from the conversation).
    outbound = Interaction(
        tenant_id=tenant.id,
        agent_id=user.id if user else None,
        contact_id=conv.contact_id,
        conversation_id=conv.id,
        channel="email",
        source=result["provider"],
        direction="outbound",
        title=body.subject,
        subject=body.subject,
        raw_text=body.body,
        thread_id=conv.thread_key,
        from_address=from_address,
        to_addresses=list(body.to),
        cc_addresses=list(body.cc),
        message_id=result.get("message_id") or None,
        in_reply_to=in_reply_to,
        references=references,
        provider_message_id=result.get("provider_message_id") or None,
        is_internal=False,
        classification=conv.classification,
        status="processing",
    )
    db.add(outbound)
    await db.flush()

    # Enqueue analysis so the agent's response gets scored by the pipeline.
    try:
        from backend.app.tasks import process_text_interaction

        process_text_interaction.delay(str(outbound.id))
    except Exception:
        pass

    conv.status = "waiting_customer"
    return {"status": "sent", "interaction_id": str(outbound.id), "provider": result["provider"]}
