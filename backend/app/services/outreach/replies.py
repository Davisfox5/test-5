"""Ingest-side outreach hooks — reply, opt-out, and bounce handling.

Called from services/email_ingest/ingest.py inside its (sync) session,
so everything here mutates state in the caller's transaction and never
commits. Webhook deliveries enqueue via dispatch_sync (rows + Celery).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Customer,
    Interaction,
    OutreachMember,
)
from backend.app.services.outreach.common import (
    detect_opt_out,
    extract_message_ids,
    looks_like_bounce,
)
from backend.app.services.outreach.scheduler import (
    ACTIVE_MEMBER_STATES,
    halt_members_for_customer,
    set_pipeline_status_sync,
)
from backend.app.services.webhook_dispatcher import dispatch_sync

logger = logging.getLogger(__name__)


def _find_member(
    session: Session,
    tenant_id: uuid.UUID,
    campaign_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    contact_id: Optional[uuid.UUID],
) -> Optional[OutreachMember]:
    stmt = select(OutreachMember).where(
        OutreachMember.tenant_id == tenant_id,
        OutreachMember.campaign_id == campaign_id,
    )
    if customer_id is not None:
        stmt = stmt.where(OutreachMember.customer_id == customer_id)
    elif contact_id is not None:
        stmt = stmt.where(OutreachMember.contact_id == contact_id)
    else:
        return None
    return session.execute(stmt).scalars().first()


def find_recipient_for_reply(
    session: Session,
    tenant_id: uuid.UUID,
    in_reply_to: Optional[str],
    references: Optional[list],
    from_address: Optional[str],
) -> Optional[CampaignRecipient]:
    """Attribute an inbound email to a tracked campaign send.

    Match order:
    1. In-Reply-To / any References id against recipient rows (Gmail
       sends — we know the Message-ID we wrote).
    2. Sender address against recipients of *outreach* campaigns whose
       member is still awaiting a reply (covers Outlook sends, where
       Graph assigns the Message-ID server-side and we never see it).
    """
    candidates = []
    if in_reply_to:
        candidates.append(in_reply_to)
    for ref in references or []:
        if ref and ref not in candidates:
            candidates.append(ref)
    if candidates:
        recipient = (
            session.execute(
                select(CampaignRecipient)
                .where(
                    CampaignRecipient.tenant_id == tenant_id,
                    CampaignRecipient.rfc822_message_id.in_(candidates),
                )
                .order_by(CampaignRecipient.sent_at.desc().nullslast())
            )
            .scalars()
            .first()
        )
        if recipient is not None:
            return recipient

    if not from_address:
        return None
    rows = (
        session.execute(
            select(CampaignRecipient, Campaign)
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(
                CampaignRecipient.tenant_id == tenant_id,
                CampaignRecipient.email_address == from_address.lower(),
                Campaign.kind == "outreach",
            )
            .order_by(CampaignRecipient.sent_at.desc().nullslast())
        )
        .all()
    )
    for recipient, _campaign in rows:
        member = _find_member(
            session, tenant_id, recipient.campaign_id,
            recipient.customer_id, recipient.contact_id,
        )
        if member is not None and member.state in ACTIVE_MEMBER_STATES + ("completed",):
            return recipient
    return None


def handle_outreach_reply(
    session: Session,
    tenant_id: uuid.UUID,
    interaction: Interaction,
    recipient: CampaignRecipient,
    contact: Optional[Contact],
) -> None:
    """An inbound reply attributed to an outreach send landed as an
    Interaction: halt the sequence, flip statuses, honor opt-outs."""
    campaign = session.get(Campaign, recipient.campaign_id)
    if campaign is None or campaign.kind != "outreach":
        return

    customer_id = recipient.customer_id or (contact.customer_id if contact else None)
    customer = session.get(Customer, customer_id) if customer_id else None
    member = _find_member(
        session, tenant_id, campaign.id, customer_id, recipient.contact_id
    )

    opted_out = detect_opt_out(interaction.raw_text)
    now = datetime.now(timezone.utc)

    if member is not None and member.state not in ("opted_out",):
        member.state = "opted_out" if opted_out else "replied"
        member.replied_at = now
        member.next_send_at = None
        member.halt_reason = "opt_out_reply" if opted_out else None

    if customer is not None:
        # Make sure late-created interactions hang off the prospect even
        # when entity resolution hasn't run yet.
        if interaction.customer_id is None:
            interaction.customer_id = customer.id
        if opted_out:
            customer.do_not_contact = True
            halt_members_for_customer(
                session, tenant_id, customer.id,
                reason="opt_out_reply",
                exclude_member_id=member.id if member else None,
            )
            set_pipeline_status_sync(
                session, customer, "do_not_contact",
                reason="opt_out_reply", campaign_id=campaign.id,
            )
        else:
            set_pipeline_status_sync(
                session, customer, "replied",
                reason="outreach_reply", campaign_id=campaign.id,
            )

    if opted_out:
        session.add(
            CampaignEvent(
                campaign_id=campaign.id,
                tenant_id=tenant_id,
                recipient_id=recipient.id,
                contact_id=recipient.contact_id,
                event_type="unsubscribe",
                metadata_={"message_id": interaction.message_id},
            )
        )

    event = "outreach.email.opted_out" if opted_out else "outreach.email.replied"
    snippet = (interaction.raw_text or "")[:500]
    try:
        dispatch_sync(
            session,
            tenant_id,
            event,
            {
                "prospect_id": str(customer.id) if customer else None,
                "prospect_name": customer.name if customer else None,
                "campaign_id": str(campaign.id),
                "campaign_name": campaign.name,
                "member_id": str(member.id) if member else None,
                "interaction_id": str(interaction.id),
                "from": interaction.from_address,
                "subject": interaction.subject,
                "snippet": snippet,
                "pipeline_status": customer.pipeline_status if customer else None,
                "occurred_at": now.isoformat(),
                "source": "reply",
            },
        )
    except Exception:
        logger.warning("%s webhook enqueue failed", event, exc_info=True)


def handle_possible_bounce(
    session: Session,
    tenant_id: uuid.UUID,
    from_address: Optional[str],
    subject: Optional[str],
    body_text: Optional[str],
    in_reply_to: Optional[str],
    references: Optional[list],
) -> bool:
    """Detect a DSN for a tracked outreach send. Returns True when handled.

    Called from ingest for messages that would otherwise be dropped as
    internal/auto-generated — a bounce never becomes an Interaction, but
    it must still mark the member and tell Flex.
    """
    if not looks_like_bounce(from_address, subject):
        return False

    candidates = []
    if in_reply_to:
        candidates.append(in_reply_to)
    for ref in references or []:
        if ref and ref not in candidates:
            candidates.append(ref)
    for mid in extract_message_ids(body_text):
        if mid not in candidates:
            candidates.append(mid)
    if not candidates:
        return False

    recipient = (
        session.execute(
            select(CampaignRecipient)
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(
                CampaignRecipient.tenant_id == tenant_id,
                CampaignRecipient.rfc822_message_id.in_(candidates),
                Campaign.kind == "outreach",
            )
        )
        .scalars()
        .first()
    )
    if recipient is None:
        return False

    campaign = session.get(Campaign, recipient.campaign_id)
    member = _find_member(
        session, tenant_id, recipient.campaign_id,
        recipient.customer_id, recipient.contact_id,
    )
    now = datetime.now(timezone.utc)
    if member is not None and member.state in ACTIVE_MEMBER_STATES + ("completed",):
        member.state = "bounced"
        member.next_send_at = None
        member.halt_reason = "bounce"

    session.add(
        CampaignEvent(
            campaign_id=recipient.campaign_id,
            tenant_id=tenant_id,
            recipient_id=recipient.id,
            contact_id=recipient.contact_id,
            event_type="bounce",
            metadata_={"subject": subject, "from": from_address},
        )
    )

    try:
        dispatch_sync(
            session,
            tenant_id,
            "outreach.email.bounced",
            {
                "prospect_id": str(recipient.customer_id) if recipient.customer_id else None,
                "campaign_id": str(recipient.campaign_id),
                "campaign_name": campaign.name if campaign else None,
                "member_id": str(member.id) if member else None,
                "to": recipient.email_address,
                "reason": (subject or "")[:200],
                "occurred_at": now.isoformat(),
            },
        )
    except Exception:
        logger.warning("outreach.email.bounced webhook enqueue failed", exc_info=True)
    return True
