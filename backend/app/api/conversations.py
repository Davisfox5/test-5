"""Conversations + AI reply drafting API.

Endpoints:
- GET  /conversations                         — list for the tenant
- GET  /conversations/{id}                    — thread detail with messages + attachments
- GET  /conversations/{id}/reply-defaults     — prefill values for reply-all
- GET  /attachments/{id}/download             — presigned S3 redirect
- POST /conversations/{id}/draft-reply        — ask Sonnet for a draft
- POST /conversations/{id}/send-reply         — send via Gmail/Graph + log
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    Conversation,
    Integration,
    Interaction,
    InteractionAttachment,
    Tenant,
    User,
)
from backend.app.services.attachment_store import get_store
from backend.app.services.email_reply import ReplyDrafter
from backend.app.services.email_send import (
    OutboundAttachment,
    send_via_gmail,
    send_via_graph,
)

router = APIRouter()


class AttachmentOut(BaseModel):
    id: uuid.UUID
    filename: Optional[str]
    content_type: Optional[str]
    size_bytes: Optional[int]
    direction: Optional[str]
    inline: bool
    has_bytes: bool

    model_config = {"from_attributes": True}


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
    cc_addresses: list
    bcc_addresses: list
    subject: Optional[str]
    raw_text: Optional[str]
    body_html: Optional[str]
    created_at: datetime
    insights: dict
    attachments: List[AttachmentOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: List[ConversationMessage]


class ReplyDefaults(BaseModel):
    """Prefilled recipient sets for the reply UI."""

    subject: str
    reply_to: List[EmailStr]
    reply_all_to: List[EmailStr]
    reply_all_cc: List[EmailStr]


class DraftReplyRequest(BaseModel):
    extra_instructions: Optional[str] = None


class DraftReplyResponse(BaseModel):
    subject: str
    body: str
    rationale: str
    citations: list
    requires_human_review: bool


class SendAttachment(BaseModel):
    """Attachment payload on send.

    Either ``attachment_id`` (re-attach a file from this conversation —
    the server fetches bytes from S3) or inline bytes via ``filename`` +
    ``content_type`` + ``data_base64``.
    """

    attachment_id: Optional[uuid.UUID] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    data_base64: Optional[str] = None


class SendReplyRequest(BaseModel):
    subject: str
    body: str
    body_html: Optional[str] = None
    to: List[EmailStr]
    cc: List[EmailStr] = Field(default_factory=list)
    bcc: List[EmailStr] = Field(default_factory=list)
    integration_id: uuid.UUID
    attachments: List[SendAttachment] = Field(default_factory=list)


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

    # One attachment query covers the whole thread.
    message_ids = [m.id for m in messages]
    attachments: List[InteractionAttachment] = []
    if message_ids:
        attachments = (await db.execute(
            select(InteractionAttachment)
            .where(InteractionAttachment.interaction_id.in_(message_ids))
        )).scalars().all()
    atts_by_msg: dict = {}
    for a in attachments:
        atts_by_msg.setdefault(a.interaction_id, []).append(
            AttachmentOut(
                id=a.id,
                filename=a.filename,
                content_type=a.content_type,
                size_bytes=a.size_bytes,
                direction=a.direction,
                inline=a.inline,
                has_bytes=bool(a.s3_key),
            )
        )

    msg_payloads: List[ConversationMessage] = []
    for m in messages:
        payload = ConversationMessage.model_validate(m).model_dump()
        payload["attachments"] = [a.model_dump() for a in atts_by_msg.get(m.id, [])]
        msg_payloads.append(ConversationMessage(**payload))

    return ConversationDetail(
        **ConversationOut.model_validate(conv).model_dump(),
        messages=msg_payloads,
    )


@router.get("/conversations/{conv_id}/reply-defaults", response_model=ReplyDefaults)
async def reply_defaults(
    conv_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the recipient lists the UI should prefill for Reply / Reply-All.

    Reply-All = everyone on the last inbound message's From + To + Cc,
    minus any address on the tenant's own mailbox.
    """
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conv_id, Conversation.tenant_id == tenant.id
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    last_inbound = (await db.execute(
        select(Interaction)
        .where(
            Interaction.conversation_id == conv_id,
            Interaction.direction == "inbound",
        )
        .order_by(Interaction.created_at.desc())
    )).scalars().first()
    if last_inbound is None:
        return ReplyDefaults(
            subject=f"Re: {conv.subject or ''}",
            reply_to=[],
            reply_all_to=[],
            reply_all_cc=[],
        )

    # Tenant's own mailbox addresses — any User email + any Integration
    # user email — so reply-all doesn't send a message to ourselves.
    own_rows = (await db.execute(
        select(User.email).where(User.tenant_id == tenant.id)
    )).all()
    own = {row[0].lower() for row in own_rows if row[0]}

    def _clean(addrs):
        out = []
        seen = set()
        for a in addrs or []:
            if not a:
                continue
            low = a.lower()
            if low in own or low in seen:
                continue
            seen.add(low)
            out.append(a)
        return out

    from_addr = last_inbound.from_address or ""
    subj = conv.subject or last_inbound.subject or ""
    return ReplyDefaults(
        subject=subj if subj.lower().startswith("re:") else f"Re: {subj}".strip(": "),
        reply_to=[from_addr] if from_addr else [],
        reply_all_to=_clean([from_addr] + list(last_inbound.to_addresses or [])),
        reply_all_cc=_clean(last_inbound.cc_addresses or []),
    )


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    att = (await db.execute(
        select(InteractionAttachment).where(
            InteractionAttachment.id == attachment_id,
            InteractionAttachment.tenant_id == tenant.id,
        )
    )).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not att.s3_key:
        raise HTTPException(status_code=404, detail="Attachment bytes not stored")
    url = get_store().presigned_url(att.s3_key)
    if not url:
        raise HTTPException(status_code=503, detail="Attachment store unavailable")
    return RedirectResponse(url=url, status_code=302)


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

    # Materialize attachments.
    outbound_atts: List[OutboundAttachment] = []
    store = get_store()
    for spec in body.attachments or []:
        att_bytes: Optional[bytes] = None
        filename = spec.filename
        content_type = spec.content_type
        if spec.attachment_id is not None:
            row = (await db.execute(
                select(InteractionAttachment).where(
                    InteractionAttachment.id == spec.attachment_id,
                    InteractionAttachment.tenant_id == tenant.id,
                )
            )).scalar_one_or_none()
            if row is None or not row.s3_key:
                raise HTTPException(
                    status_code=400,
                    detail=f"Attachment {spec.attachment_id} unavailable",
                )
            fetched = store.get(row.s3_key)
            if not fetched:
                raise HTTPException(status_code=503, detail="Attachment store unavailable")
            att_bytes, fetched_ct = fetched
            filename = filename or row.filename or "attachment"
            content_type = content_type or row.content_type or fetched_ct
        elif spec.data_base64:
            try:
                att_bytes = base64.b64decode(spec.data_base64)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid attachment data_base64")
            filename = filename or "attachment"
        else:
            continue

        outbound_atts.append(OutboundAttachment(
            filename=filename or "attachment",
            content_type=content_type,
            data=att_bytes or b"",
        ))

    from backend.app.api.oauth import get_provider_token

    access_token = await get_provider_token(db, integration)

    send_kwargs = dict(
        access_token=access_token,
        from_address=from_address,
        to=list(body.to),
        cc=list(body.cc),
        bcc=list(body.bcc),
        subject=body.subject,
        body=body.body,
        body_html=body.body_html,
        in_reply_to=in_reply_to,
        references=references,
        attachments=outbound_atts or None,
    )
    if integration.provider == "google":
        result = send_via_gmail(**send_kwargs)
    elif integration.provider == "microsoft":
        result = send_via_graph(**send_kwargs)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {integration.provider}")

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
        body_html=body.body_html,
        thread_id=conv.thread_key,
        from_address=from_address,
        to_addresses=list(body.to),
        cc_addresses=list(body.cc),
        bcc_addresses=list(body.bcc),
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

    # Mirror outbound attachments back onto the Interaction row so the
    # thread shows what we sent.
    for spec, mat in zip(body.attachments or [], outbound_atts):
        db.add(InteractionAttachment(
            interaction_id=outbound.id,
            tenant_id=tenant.id,
            filename=mat.filename,
            content_type=mat.content_type,
            size_bytes=len(mat.data),
            # Reuse existing s3_key on re-attach; otherwise upload a fresh copy.
            s3_key=_maybe_upload(store, tenant.id, outbound.id, mat),
            direction="outbound",
            inline=False,
        ))

    try:
        from backend.app.tasks import process_text_interaction

        process_text_interaction.delay(str(outbound.id))
    except Exception:
        pass

    conv.status = "waiting_customer"
    return {"status": "sent", "interaction_id": str(outbound.id), "provider": result["provider"]}


def _maybe_upload(store, tenant_id, interaction_id, mat: OutboundAttachment) -> Optional[str]:
    return store.put(
        tenant_id=tenant_id,
        interaction_id=interaction_id,
        filename=mat.filename,
        content_type=mat.content_type,
        data=mat.data,
    )
