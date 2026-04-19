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
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.db import get_db
from backend.app.models import (
    Contact,
    EmailSend,
    Integration,
    Interaction,
)
from backend.app.services.email.base import (
    EmailAuthError,
    EmailSender,
    EmailSendError,
)
from backend.app.services.email.gmail import GmailSender
from backend.app.services.email.outlook import OutlookSender
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────


class EmailSendIn(BaseModel):
    to: EmailStr
    subject: str = Field(..., min_length=1, max_length=400)
    body: str = Field(..., min_length=1)
    cc: Optional[EmailStr] = None
    # Force a specific provider; otherwise we pick whichever the caller has
    # connected (preferring google if both).
    provider: Optional[Literal["google", "microsoft"]] = None


class EmailSendOut(BaseModel):
    id: uuid.UUID
    interaction_id: Optional[uuid.UUID]
    provider: str
    to_address: str
    cc_address: Optional[str]
    subject: str
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

    record = EmailSend(
        tenant_id=principal.tenant.id,
        interaction_id=interaction_id,
        sender_user_id=principal.user_id,
        provider=integ.provider,
        to_address=body.to,
        cc_address=body.cc,
        subject=body.subject,
        body=body.body,
        status="pending",
    )
    db.add(record)
    await db.flush()

    sender = _build_sender(integ, principal_email_hint=_principal_email(principal))

    try:
        result = await sender.send(
            to=body.to, subject=body.subject, body=body.body, cc=body.cc
        )
        record.status = "sent"
        record.provider_message_id = result.message_id
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
