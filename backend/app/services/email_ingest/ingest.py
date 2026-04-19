"""Shared ingestion logic — classify → thread → persist → enqueue.

The Gmail and Graph fetchers produce a provider-neutral
:class:`NormalizedEmail`.  This module takes one of those and:

1. Runs the internal/external classifier.  Internal/low-confidence
   emails are dropped with a log line and NEVER create an Interaction
   row.
2. Resolves or creates the ``Conversation`` row (thread key derived
   from RFC-822 headers with a subject-based fallback).
3. Upserts a ``Contact`` based on the counterparty's email.
4. Creates an ``Interaction`` row (channel=email) and attaches it to
   the conversation.
5. Enqueues ``process_text_interaction`` so the shared analysis
   pipeline runs.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.app.models import (
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Conversation,
    Interaction,
    Tenant,
    User,
)
from backend.app.services.email_classifier import (
    EmailClassifier,
    EmailForClassification,
)

logger = logging.getLogger(__name__)


@dataclass
class NormalizedEmail:
    """Provider-agnostic representation of a single email."""

    provider: str
    provider_message_id: str
    message_id: str  # RFC-822 Message-ID
    in_reply_to: Optional[str]
    references: List[str] = field(default_factory=list)
    subject: Optional[str] = None
    from_address: str = ""
    to_addresses: List[str] = field(default_factory=list)
    cc_addresses: List[str] = field(default_factory=list)
    body_text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    received_at: Optional[datetime] = None
    direction: str = "inbound"  # inbound|outbound — based on folder/label
    agent_email: Optional[str] = None  # email of the authenticated mailbox


def _tenant_domains(tenant: Tenant) -> List[str]:
    """Pull internal-domain list out of tenant.features_enabled/metadata."""
    feats = tenant.features_enabled or {}
    domains = feats.get("email_internal_domains") or []
    if isinstance(domains, str):
        domains = [d.strip() for d in domains.split(",") if d.strip()]
    return list(domains)


def _thread_key(email: NormalizedEmail) -> str:
    """Derive a stable grouping key from thread headers.

    Gmail and Graph both preserve RFC-822 References; we take the
    first Message-ID in the chain, falling back to In-Reply-To, then
    to a hash of (counterparty, normalized-subject).
    """
    if email.references:
        return email.references[0]
    if email.in_reply_to:
        return email.in_reply_to
    subject = (email.subject or "").lower()
    for prefix in ("re: ", "fwd: ", "fw: "):
        while subject.startswith(prefix):
            subject = subject[len(prefix):]
    counterparty = email.from_address.lower()
    return "subj:" + hashlib.sha1(
        f"{counterparty}|{subject}".encode("utf-8")
    ).hexdigest()


def _counterparty_address(email: NormalizedEmail) -> str:
    """The external party — for inbound it's from, for outbound it's the first external to."""
    if email.direction == "inbound":
        return email.from_address
    for addr in email.to_addresses:
        if addr and addr.lower() != (email.agent_email or "").lower():
            return addr
    return email.to_addresses[0] if email.to_addresses else ""


def _upsert_contact(session: Session, tenant_id: uuid.UUID, address: str) -> Optional[Contact]:
    if not address:
        return None
    contact = (
        session.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.email == address)
        .first()
    )
    if contact is None:
        contact = Contact(tenant_id=tenant_id, email=address, name=address.split("@")[0])
        session.add(contact)
        session.flush()
    return contact


def _upsert_conversation(
    session: Session,
    tenant_id: uuid.UUID,
    thread_key: str,
    subject: Optional[str],
    classification: str,
    contact_id: Optional[uuid.UUID],
    received_at: Optional[datetime],
) -> Conversation:
    conv = (
        session.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.thread_key == thread_key,
        )
        .first()
    )
    if conv is None:
        conv = Conversation(
            tenant_id=tenant_id,
            thread_key=thread_key,
            channel="email",
            subject=subject,
            classification=classification,
            contact_id=contact_id,
            status="open",
        )
        session.add(conv)
        session.flush()
    # Always bump counters on every appended message.
    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = received_at or datetime.now(timezone.utc)
    # Classification only gets upgraded from 'other' when a confident bucket shows up.
    if classification != "other" and (conv.classification in (None, "other")):
        conv.classification = classification
    return conv


def _resolve_agent(session: Session, tenant_id: uuid.UUID, email: Optional[str]) -> Optional[User]:
    if not email:
        return None
    return (
        session.query(User)
        .filter(User.tenant_id == tenant_id, User.email == email)
        .first()
    )


async def ingest_email(
    session: Session,
    tenant: Tenant,
    email: NormalizedEmail,
    classifier: Optional[EmailClassifier] = None,
) -> Optional[uuid.UUID]:
    """Process a single email.  Returns the Interaction id, or None if filtered.

    Idempotent on ``provider_message_id`` + RFC-822 Message-ID so the
    poller can re-run a window without creating duplicates.
    """
    # Dedupe — Gmail/Graph poll windows overlap.
    existing = (
        session.query(Interaction)
        .filter(
            Interaction.tenant_id == tenant.id,
            Interaction.provider_message_id == email.provider_message_id,
        )
        .first()
    )
    if existing:
        return existing.id

    classifier = classifier or EmailClassifier()

    verdict = await classifier.classify(
        EmailForClassification(
            subject=email.subject,
            from_address=email.from_address,
            to_addresses=email.to_addresses,
            cc_addresses=email.cc_addresses,
            body_preview=email.body_text,
            headers=email.headers,
            tenant_domains=_tenant_domains(tenant),
        )
    )

    if not verdict.is_external:
        logger.info(
            "Skipping internal email msgid=%s reason=%s",
            email.message_id, verdict.reason,
        )
        return None

    counterparty = _counterparty_address(email)
    contact = _upsert_contact(session, tenant.id, counterparty)
    agent = _resolve_agent(session, tenant.id, email.agent_email)

    # Campaign attribution: if this is an inbound reply to a tracked
    # campaign send, link it and record a reply event so campaign
    # analytics stay fresh without a separate pass.
    campaign_id = None
    if email.direction == "inbound" and email.in_reply_to:
        recipient = (
            session.query(CampaignRecipient)
            .filter(
                CampaignRecipient.tenant_id == tenant.id,
                CampaignRecipient.rfc822_message_id == email.in_reply_to,
            )
            .first()
        )
        if recipient is not None:
            campaign_id = recipient.campaign_id
            session.add(
                CampaignEvent(
                    campaign_id=recipient.campaign_id,
                    tenant_id=tenant.id,
                    recipient_id=recipient.id,
                    contact_id=contact.id if contact else None,
                    event_type="reply",
                    metadata_={"message_id": email.message_id},
                )
            )

    thread_key = _thread_key(email)
    conversation = _upsert_conversation(
        session=session,
        tenant_id=tenant.id,
        thread_key=thread_key,
        subject=email.subject,
        classification=verdict.classification,
        contact_id=contact.id if contact else None,
        received_at=email.received_at,
    )

    interaction = Interaction(
        tenant_id=tenant.id,
        agent_id=agent.id if agent else None,
        contact_id=contact.id if contact else None,
        conversation_id=conversation.id,
        campaign_id=campaign_id,
        channel="email",
        source=email.provider,
        direction=email.direction,
        title=email.subject,
        raw_text=email.body_text,
        thread_id=thread_key,
        subject=email.subject,
        from_address=email.from_address,
        to_addresses=email.to_addresses,
        cc_addresses=email.cc_addresses,
        message_id=email.message_id,
        in_reply_to=email.in_reply_to,
        references=email.references,
        provider_message_id=email.provider_message_id,
        is_internal=False,
        classification=verdict.classification,
        classification_confidence=verdict.confidence,
        status="processing",
    )
    session.add(interaction)
    session.flush()

    # Enqueue analysis — Celery is optional in dev/test environments.
    try:
        from backend.app.tasks import process_text_interaction

        process_text_interaction.delay(str(interaction.id))
    except Exception:  # pragma: no cover
        logger.exception("Failed to enqueue text pipeline; analysis deferred")

    return interaction.id
