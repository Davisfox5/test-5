"""Follow-up email API.

Ties together:

* The post-call analysis artifacts (``Interaction.insights.follow_up_email_draft``
  and ``action_items[].suggested_email_draft``).
* The stored Gmail/Outlook OAuth tokens (``Integration`` rows).
* The per-user principal (who's actually sending).

Endpoints:

* ``GET  /interactions/{id}/follow-up-draft`` — render the draft LINDA
  produced, plus a recent-sends history for the interaction.
* ``POST /interactions/{id}/send-follow-up`` — send the draft (optionally
  edited) via the caller's preferred provider. Logs an ``EmailSend`` row
  either way.
* ``GET  /emails`` — tenant-wide outbox (manager+) for the SPA's
  Communications page, with status / date / search filters and
  pagination. Joins Interaction + User so the table renders without a
  fan-out.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal, require_role
from backend.app.db import get_db
from backend.app.models import (
    Contact,
    EmailSend,
    Integration,
    Interaction,
    User,
)
from backend.app.services.email.base import (
    EmailAuthError,
    EmailSender,
    EmailSendError,
    OutboundAttachment,
)
from backend.app.services.email.gmail import GmailSender
from backend.app.services.email.outlook import OutlookSender
from backend.app.services.token_crypto import decrypt_token, encrypt_token
from backend.app.services.llm_client import model_for_tier

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────


class EmailAttachmentIn(BaseModel):
    """An attachment to include with the send. v1 supports KB doc
    references; binary upload IDs slot in here when file storage lands.
    """
    kind: Literal["kb", "upload"] = "kb"
    id: str  # kb_doc_id (UUID string) or upload_id
    title: Optional[str] = None  # falls back to KB doc title when omitted
    mime_type: Optional[str] = None


class EmailSendIn(BaseModel):
    to: EmailStr
    subject: str = Field(..., min_length=1, max_length=400)
    body: str = Field(..., min_length=1)
    cc: Optional[EmailStr] = None
    # Force a specific provider; otherwise we pick whichever the caller has
    # connected (preferring google if both).
    provider: Optional[Literal["google", "microsoft"]] = None
    # Optional list of attachments. v1 persists metadata + appends doc
    # references to the email body as a footer. Real MIME multipart
    # attachment sending lands in a follow-up PR — the UI flow + audit
    # trail ship now so reps can pick + review docs and the rep gets a
    # clean record of what they sent.
    attachments: List[EmailAttachmentIn] = Field(default_factory=list)


class EmailSendOut(BaseModel):
    id: uuid.UUID
    interaction_id: Optional[uuid.UUID]
    provider: str
    to_address: str
    cc_address: Optional[str]
    subject: str
    attachments: List[dict] = Field(default_factory=list)
    status: str
    provider_message_id: Optional[str]
    error: Optional[str]
    sent_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class FollowUpDraftOut(BaseModel):
    interaction_id: uuid.UUID
    suggested_to: Optional[str]
    draft_subject: str
    draft_body: str
    action_item_drafts: List[dict]
    recent_sends: List[EmailSendOut]


class EmailSendListItem(BaseModel):
    """Outbox row for the tenant-wide /emails listing.

    Embeds the small handful of join fields the UI needs so it doesn't
    have to fan out to the interactions / users endpoints per row.
    """

    id: uuid.UUID
    interaction_id: Optional[uuid.UUID]
    interaction_title: Optional[str]
    interaction_channel: Optional[str]
    sender_user_id: Optional[uuid.UUID]
    sender_name: Optional[str]
    sender_email: Optional[str]
    provider: str
    to_address: str
    cc_address: Optional[str]
    subject: str
    body: str
    status: str
    provider_message_id: Optional[str]
    error: Optional[str]
    sent_at: Optional[datetime]
    created_at: datetime


class EmailSendListOut(BaseModel):
    items: List[EmailSendListItem]
    total: int
    limit: int
    offset: int


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get(
    "/interactions/{interaction_id}/follow-up-draft",
    response_model=FollowUpDraftOut,
)
async def get_follow_up_draft(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Render the AI-drafted follow-up email + any action-item email
    drafts + the send history for this interaction."""
    interaction = await db.get(Interaction, interaction_id)
    if interaction is None or interaction.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Interaction not found")

    insights = interaction.insights or {}
    draft = insights.get("follow_up_email_draft") or {}

    # Infer a recipient from the linked contact's email if we have one —
    # the caller can always override in the send request.
    suggested_to: Optional[str] = None
    if interaction.contact_id:
        contact = await db.get(Contact, interaction.contact_id)
        if contact and contact.email:
            suggested_to = contact.email

    action_items = insights.get("action_items") or []
    ai_drafts = [
        {
            "title": item.get("title", ""),
            "category": item.get("category"),
            "priority": item.get("priority"),
            "draft": item.get("suggested_email_draft"),
        }
        for item in action_items
        if isinstance(item, dict) and item.get("suggested_email_draft")
    ]

    stmt = (
        select(EmailSend)
        .where(EmailSend.interaction_id == interaction_id)
        .order_by(EmailSend.created_at.desc())
        .limit(20)
    )
    recent = list((await db.execute(stmt)).scalars().all())

    return FollowUpDraftOut(
        interaction_id=interaction_id,
        suggested_to=suggested_to,
        draft_subject=str(draft.get("subject") or "")[:400],
        draft_body=str(draft.get("body") or ""),
        action_item_drafts=ai_drafts,
        recent_sends=[EmailSendOut.model_validate(r) for r in recent],
    )


@router.post(
    "/interactions/{interaction_id}/follow-up-draft/regenerate",
    response_model=FollowUpDraftOut,
)
async def regenerate_follow_up_draft(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Regenerate just the follow-up email draft.

    The SPA's Regenerate button hits this. We make one focused Sonnet
    call (the call summary + action items go in; just a fresh
    ``follow_up_email_draft`` comes out) so the rep doesn't pay for a
    full re-analysis. The new draft replaces the existing
    ``insights.follow_up_email_draft`` in place.
    """
    interaction = await db.get(Interaction, interaction_id)
    if interaction is None or interaction.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Interaction not found")

    insights = dict(interaction.insights or {})
    summary = str(insights.get("summary") or "")
    if not summary:
        raise HTTPException(
            status_code=400,
            detail=(
                "No analysis available for this interaction yet. Wait for the "
                "pipeline to finish or trigger /redrive."
            ),
        )

    # Build a focused user message: summary + action items + any
    # methodology context. The narrow scope lets the model spend
    # generation budget on prose quality rather than re-deriving the
    # whole analysis.
    action_items = insights.get("action_items") or []
    ai_lines: List[str] = []
    for item in action_items[:8]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        due = item.get("due_date") or "no due date"
        ai_lines.append(f"- {title} (due: {due})")

    contact_name: Optional[str] = None
    if interaction.contact_id:
        contact = await db.get(Contact, interaction.contact_id)
        if contact:
            contact_name = contact.first_name or contact.name or contact.email

    from datetime import date as _date
    call_dt = getattr(interaction, "started_at", None) or interaction.created_at
    call_date_str = call_dt.date().isoformat() if call_dt else _date.today().isoformat()

    from backend.app.services.ai_analysis import (
        ANALYSIS_SYSTEM_PROMPT_TERSE,
    )
    from backend.app.services.llm_client import (
        compute_max_tokens,
        get_async_anthropic,
    )

    # Tiny focused prompt. The full ANALYSIS_SYSTEM_PROMPT_TERSE carries
    # the EMAIL DRAFT VOICE block we want enforced; we ask only for the
    # draft field, not the full schema.
    system_prompt = (
        "You are rewriting just one section of a sales-call analysis: the "
        "``follow_up_email_draft``. Apply the EMAIL DRAFT VOICE rules from "
        "the analyst-instructions block exactly. The call already happened "
        "and was analyzed; you have the summary + action items below. "
        "Return ONLY a JSON object {\"subject\": str, \"body\": str}. No "
        "markdown fences.\n\n"
        + ANALYSIS_SYSTEM_PROMPT_TERSE
    )

    user_content = (
        f"## Call Date\n{call_date_str}\n\n"
        f"## Recipient\n{contact_name or 'the customer'}\n\n"
        f"## Call Summary\n{summary}\n\n"
        f"## Action Items\n" + ("\n".join(ai_lines) if ai_lines else "(none)") + "\n\n"
        "Write the follow-up email now. Subject + body only, as JSON."
    )

    client = get_async_anthropic()
    budget = compute_max_tokens("sonnet", input_tokens=len(user_content) // 4)
    response = await client.messages.create(
        model=model_for_tier("sonnet"),
        max_tokens=budget,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )
    raw_text = response.content[0].text

    import json as _json
    from backend.app.services.ai_analysis import (
        _strip_dashes,
        _scrub_str,
    )
    from backend.app.services.triage_service import _strip_json_fences

    try:
        new_draft = _json.loads(_strip_json_fences(raw_text))
    except _json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="LLM returned malformed JSON; try again or edit the existing draft.",
        )
    if not isinstance(new_draft, dict):
        raise HTTPException(status_code=502, detail="LLM returned unexpected payload.")

    # Scrub dashes the same way as the main pipeline.
    _strip_dashes(new_draft)

    insights["follow_up_email_draft"] = {
        "subject": str(new_draft.get("subject") or "").strip()[:400],
        "body": _scrub_str(str(new_draft.get("body") or "")),
    }
    interaction.insights = insights

    # Explicit UPDATE for the JSONB column. SQLAlchemy's dirty-attribute
    # tracking is unreliable for in-place JSONB mutations; this matches
    # the pattern used elsewhere in the worker.
    from sqlalchemy import update as _sql_update
    await db.execute(
        _sql_update(Interaction)
        .where(Interaction.id == interaction.id)
        .values(insights=insights)
    )
    await db.commit()

    # Re-emit the full draft payload so the SPA can hydrate without a
    # second fetch.
    return await get_follow_up_draft(
        interaction_id=interaction_id, db=db, principal=principal,
    )


@router.delete(
    "/interactions/{interaction_id}/follow-up-draft",
    status_code=204,
)
async def discard_follow_up_draft(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Discard the saved follow-up draft on this interaction.

    Removes ``insights.follow_up_email_draft`` so the SPA stops
    rendering it. The action_item-level drafts are untouched.
    """
    interaction = await db.get(Interaction, interaction_id)
    if interaction is None or interaction.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Interaction not found")

    insights = dict(interaction.insights or {})
    if "follow_up_email_draft" in insights:
        del insights["follow_up_email_draft"]
        interaction.insights = insights
        from sqlalchemy import update as _sql_update
        await db.execute(
            _sql_update(Interaction)
            .where(Interaction.id == interaction.id)
            .values(insights=insights)
        )
        await db.commit()


@router.post(
    "/interactions/{interaction_id}/send-follow-up",
    response_model=EmailSendOut,
    status_code=201,
)
async def send_follow_up(
    interaction_id: uuid.UUID,
    body: EmailSendIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Send the follow-up email via the caller's connected Gmail/Outlook.

    Writes a pending EmailSend row BEFORE calling the provider, then
    updates it to ``sent`` or ``failed`` based on the provider response.
    This gives us a durable audit even if the provider HTTP call hangs
    or crashes the worker mid-flight.
    """
    interaction = await db.get(Interaction, interaction_id)
    if interaction is None or interaction.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Interaction not found")

    integ = await _resolve_integration(db, principal.tenant.id, body.provider)
    if integ is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Gmail or Outlook integration connected. Connect one in "
                "Integrations, then try again."
            ),
        )

    # Resolve attachments — fetch KB doc content as real binary
    # attachments and send via the provider's MIME multipart / Graph
    # ``fileAttachment`` flows. The metadata is persisted on
    # ``email_sends.attachments`` for audit + dedupe.
    attachment_records: List[dict] = []
    outbound_attachments: List[OutboundAttachment] = []
    if body.attachments:
        from backend.app.models import KBDocument
        for att in body.attachments:
            record_meta: dict = {
                "kind": att.kind,
                "id": att.id,
                "title": att.title,
                "mime_type": att.mime_type,
            }
            if att.kind == "kb":
                try:
                    kb_id = uuid.UUID(att.id)
                except (ValueError, TypeError):
                    record_meta["resolution_error"] = "invalid_uuid"
                else:
                    kb_doc = await db.get(KBDocument, kb_id)
                    if kb_doc and kb_doc.tenant_id == principal.tenant.id:
                        record_meta["title"] = att.title or kb_doc.title
                        record_meta["source_url"] = kb_doc.source_url
                        # KB docs hold text content. Attach as a ``.txt``
                        # file so the recipient sees the source material
                        # alongside the email. Binary uploads (PDFs, etc.)
                        # need a separate file storage layer — out of
                        # scope for v1.
                        title = (
                            att.title
                            or kb_doc.title
                            or f"document-{kb_doc.id}"
                        )
                        safe_filename = _safe_filename(title) + ".txt"
                        content = (kb_doc.content or "").encode("utf-8")
                        outbound_attachments.append(
                            OutboundAttachment(
                                filename=safe_filename,
                                content_type=att.mime_type or "text/plain",
                                data=content,
                            )
                        )
                        record_meta["filename"] = safe_filename
                        record_meta["size_bytes"] = len(content)
                    else:
                        record_meta["resolution_error"] = "kb_doc_not_found"
            attachment_records.append(record_meta)

    record = EmailSend(
        tenant_id=principal.tenant.id,
        interaction_id=interaction_id,
        sender_user_id=principal.user_id,
        provider=integ.provider,
        to_address=body.to,
        cc_address=body.cc,
        subject=body.subject,
        body=body.body,
        attachments=attachment_records,
        status="pending",
    )
    db.add(record)
    await db.flush()

    sender = _build_sender(integ, principal_email_hint=_principal_email(principal))

    try:
        result = await sender.send(
            to=[body.to],
            subject=body.subject,
            body=body.body,
            cc=[body.cc] if body.cc else None,
            attachments=outbound_attachments or None,
        )
        record.status = "sent"
        record.provider_message_id = result.provider_message_id or result.message_id
        record.sent_at = datetime.now(timezone.utc)
    except EmailAuthError as exc:
        record.status = "failed"
        record.error = f"auth: {exc}"[:500]
        await _close_sender(sender)
        raise HTTPException(status_code=401, detail=str(exc))
    except EmailSendError as exc:
        record.status = "failed"
        record.error = str(exc)[:500]
        await _close_sender(sender)
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        await _close_sender(sender)

    return EmailSendOut.model_validate(record)


@router.get("/emails", response_model=EmailSendListOut)
async def list_emails(
    status: Optional[Literal["sent", "failed", "pending"]] = None,
    date_from: Optional[datetime] = Query(None, alias="date_from"),
    date_to: Optional[datetime] = Query(None, alias="date_to"),
    q: Optional[str] = Query(None, description="Search by recipient or subject"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("manager")),
) -> EmailSendListOut:
    """Tenant-wide outbox of follow-up email sends.

    Manager+ scoped — agents shouldn't be able to see the whole
    tenant's outgoing email history. Always filtered by
    ``principal.tenant.id``; cross-tenant rows are never returned.

    Joins :class:`Interaction` (title + channel for context) and
    :class:`User` (sender's name/email) so the SPA can render the
    table in one round-trip.
    """
    base_filters = [EmailSend.tenant_id == principal.tenant.id]
    if status is not None:
        base_filters.append(EmailSend.status == status)
    if date_from is not None:
        base_filters.append(EmailSend.created_at >= date_from)
    if date_to is not None:
        base_filters.append(EmailSend.created_at <= date_to)
    if q:
        needle = f"%{q.lower()}%"
        base_filters.append(
            or_(
                func.lower(EmailSend.to_address).like(needle),
                func.lower(EmailSend.subject).like(needle),
            )
        )

    # Total — separate query so pagination metadata stays accurate even
    # when the page is short.
    count_stmt = select(func.count()).select_from(EmailSend).where(*base_filters)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = (
        select(EmailSend, Interaction, User)
        .where(*base_filters)
        .join(Interaction, Interaction.id == EmailSend.interaction_id, isouter=True)
        .join(User, User.id == EmailSend.sender_user_id, isouter=True)
        .order_by(EmailSend.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()

    items: List[EmailSendListItem] = []
    for send, interaction, user in rows:
        items.append(
            EmailSendListItem(
                id=send.id,
                interaction_id=send.interaction_id,
                interaction_title=interaction.title if interaction else None,
                interaction_channel=interaction.channel if interaction else None,
                sender_user_id=send.sender_user_id,
                sender_name=user.name if user else None,
                sender_email=user.email if user else None,
                provider=send.provider,
                to_address=send.to_address,
                cc_address=send.cc_address,
                subject=send.subject,
                body=send.body,
                status=send.status,
                provider_message_id=send.provider_message_id,
                error=send.error,
                sent_at=send.sent_at,
                created_at=send.created_at,
            )
        )

    return EmailSendListOut(items=items, total=int(total), limit=limit, offset=offset)


# ── Helpers ────────────────────────────────────────────────────────────


async def _resolve_integration(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    preferred: Optional[str],
) -> Optional[Integration]:
    """Pick a connected email provider for the tenant.

    When ``preferred`` is given we return only that provider's integration
    or ``None``. Otherwise we prefer Google (larger install base) before
    Microsoft, falling back to whichever exists.
    """
    providers = [preferred] if preferred else ["google", "microsoft"]
    for p in providers:
        if p is None:
            continue
        stmt = (
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id,
                Integration.provider == p,
            )
            .order_by(Integration.created_at.desc())
            .limit(1)
        )
        integ = (await db.execute(stmt)).scalar_one_or_none()
        if integ is not None:
            return integ
    return None


def _principal_email(principal: AuthPrincipal) -> Optional[str]:
    """Best-effort From address. API-key callers have no user email — we
    fall through to the provider's authenticated mailbox."""
    return principal.user.email if principal.user else None


def _build_sender(integ: Integration, principal_email_hint: Optional[str]) -> EmailSender:
    """Decrypt the stored tokens and build the right sender.

    The ``on_token_refresh`` callback re-encrypts + writes refreshed
    tokens back onto the Integration row so the next send doesn't start
    stale.
    """
    access = decrypt_token(integ.access_token) or ""
    refresh = decrypt_token(integ.refresh_token)

    async def _on_refresh(
        new_access: str,
        new_refresh: Optional[str],
        expires_in: Optional[int],
    ) -> None:
        integ.access_token = encrypt_token(new_access)
        if new_refresh:
            integ.refresh_token = encrypt_token(new_refresh)
        if expires_in:
            from datetime import timedelta as _td

            integ.expires_at = datetime.now(timezone.utc) + _td(seconds=int(expires_in))

    if integ.provider == "google":
        return GmailSender(
            access_token=access,
            refresh_token=refresh,
            from_address=principal_email_hint or "",
            on_token_refresh=_on_refresh,
        )
    if integ.provider == "microsoft":
        return OutlookSender(
            access_token=access,
            refresh_token=refresh,
            from_address=principal_email_hint,
            on_token_refresh=_on_refresh,
        )
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported email provider on integration: {integ.provider}",
    )


async def _close_sender(sender) -> None:
    try:
        await sender.close()
    except Exception:  # pragma: no cover — defensive
        logger.debug("sender.close failed", exc_info=True)


def _safe_filename(raw: str) -> str:
    """Reduce a free-form title to a filename safe for Gmail / Graph
    multipart attachments. Strips path traversal, control chars, and
    most punctuation; collapses to underscore. Caps at 80 chars to keep
    headers within RFC 2822 line-length limits."""
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", raw or "")
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    if not cleaned:
        cleaned = "attachment"
    return cleaned[:80]
