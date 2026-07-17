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
from sqlalchemy.orm.attributes import flag_modified

from backend.app.models import (
    CampaignEvent,
    CampaignRecipient,
    Contact,
    Conversation,
    Interaction,
    InteractionAttachment,
    Tenant,
    User,
)
from backend.app.services.attachment_store import get_store
from backend.app.services.email_classifier import (
    SYSTEM_PROMPT as _CLASSIFIER_FALLBACK_PROMPT,
    EmailClassifier,
    EmailForClassification,
)
from backend.app.services.outreach import replies as outreach_replies
from backend.app.services.prompt_variant_service import (
    merge_variant_insight,
    select_variant_sync,
)

logger = logging.getLogger(__name__)


@dataclass
class NormalizedAttachment:
    """Provider-agnostic attachment metadata.  ``data`` is lazy — set by
    the fetcher after a second API call so we don't buy bytes for rows
    we're about to filter out as internal."""

    filename: str
    content_type: Optional[str]
    size_bytes: Optional[int]
    provider_attachment_id: Optional[str] = None
    content_id: Optional[str] = None  # CID for inline references
    inline: bool = False
    data: Optional[bytes] = None  # populated on demand — see fetchers


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
    bcc_addresses: List[str] = field(default_factory=list)
    body_text: str = ""
    body_html: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    received_at: Optional[datetime] = None
    direction: str = "inbound"  # inbound|outbound — based on folder/label
    agent_email: Optional[str] = None  # email of the authenticated mailbox
    attachments: List[NormalizedAttachment] = field(default_factory=list)
    # Callback to fetch bytes on demand; the ingest path only calls this
    # after the classifier says the email is external.
    attachment_fetcher: Optional[callable] = None  # type: ignore[assignment]


@dataclass
class IngestCaches:
    """Per-job lookup caches for hot bulk paths (the email backfill).

    :func:`ingest_email`'s contact/conversation upserts each cost a
    SELECT per message; a bulk job importing a 50-message thread repeats
    the identical lookups 50 times. A caller that processes many
    messages for ONE tenant inside ONE session may pass an instance to
    reuse the rows across messages.

    Callers must ``clear()`` after any rollback — the cached instances
    may reference rows the rollback discarded.
    """

    contacts: Dict[str, Contact] = field(default_factory=dict)  # key: address
    conversations: Dict[str, Conversation] = field(default_factory=dict)  # key: thread_key

    def clear(self) -> None:
        self.contacts.clear()
        self.conversations.clear()


# Free/public email providers can never be a tenant's "internal" domain —
# a customer and the tenant's own user can both be on gmail.com. We exclude
# these when auto-deriving internal domains so we never mark a public
# provider internal (which would silently drop every prospect on it).
PUBLIC_EMAIL_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
    "mail.com", "yandex.com", "zoho.com", "fastmail.com", "hey.com",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "qq.com",
})


def _domain_of(addr: Optional[str]) -> str:
    addr = (addr or "").lower()
    if "<" in addr and ">" in addr:
        addr = addr.split("<", 1)[1].split(">", 1)[0]
    return addr.split("@")[-1].strip() if "@" in addr else ""


def _tenant_domains(
    tenant: Tenant, session: Optional[Session] = None
) -> List[str]:
    """Internal ("our own") email domains for a tenant.

    Union of two sources:

    1. Any domains an admin explicitly set in
       ``features_enabled['email_internal_domains']``.
    2. **Auto-derived** from the tenant's own users' email addresses,
       excluding public providers (see ``PUBLIC_EMAIL_PROVIDERS``).

    Source 2 is why classification works with **zero setup** for every
    tenant, current and future: the people who log in to a tenant are
    that company's employees, so their (non-public) email domains are
    the company's internal domains. Without this, a fresh tenant has no
    internal domains, the deterministic prefilter can't run, and every
    email falls back to the LLM — which fails closed on any error. A
    tenant whose users are all on public providers derives nothing here
    (same as before — no regression), which is correct: for them
    "internal vs external" genuinely can't be decided by domain.

    Result is memoized on the tenant instance for the life of the poll
    (not persisted) so a 50-message batch does one users query, not 50.
    """
    cached = getattr(tenant, "_internal_domains_cache", None)
    if cached is not None:
        return cached

    feats = tenant.features_enabled or {}
    configured = feats.get("email_internal_domains") or []
    if isinstance(configured, str):
        configured = [d.strip() for d in configured.split(",") if d.strip()]
    result = {d.lower().lstrip("@").strip() for d in configured if d}

    if session is not None:
        try:
            rows = (
                session.query(User.email)
                .filter(User.tenant_id == tenant.id, User.email.isnot(None))
                .all()
            )
            for (email,) in rows:
                dom = _domain_of(email)
                if dom and dom not in PUBLIC_EMAIL_PROVIDERS:
                    result.add(dom)
        except Exception:  # noqa: BLE001 — derivation is best-effort
            logger.warning(
                "Failed to derive internal domains from users for tenant %s",
                tenant.id, exc_info=True,
            )

    out = sorted(result)
    try:
        tenant._internal_domains_cache = out
    except Exception:  # noqa: BLE001 — caching is opportunistic
        pass
    return out


def _learn_internal_domain_from_outbound(
    session: Session, tenant: Tenant, email: NormalizedEmail
) -> None:
    """Self-learn a tenant's internal domain from its OWN outbound mail.

    A SENT-folder message is, by definition, sent by the tenant, so its
    From domain is the company's own (internal). Persisting it covers the
    one class of tenant ``_tenant_domains`` can't derive from: API-key-only
    tenants that have **no seat users** (a login-less console). After their
    first outbound sync their domain is known and classification works with
    zero configuration — the truly universal, self-healing path.

    Public providers are skipped (a tenant sending from gmail.com must not
    mark gmail internal), and we only write when the domain is genuinely
    new, so steady state is a cheap membership check with no write.
    """
    if getattr(email, "direction", None) != "outbound":
        return
    dom = _domain_of(email.from_address)
    if not dom or dom in PUBLIC_EMAIL_PROVIDERS:
        return
    if dom in set(_tenant_domains(tenant, session)):
        return

    feats = dict(tenant.features_enabled or {})
    configured = feats.get("email_internal_domains") or []
    if isinstance(configured, str):
        configured = [d.strip() for d in configured.split(",") if d.strip()]
    if dom in {d.lower().lstrip("@").strip() for d in configured if d}:
        return

    feats["email_internal_domains"] = list(configured) + [dom]
    tenant.features_enabled = feats  # reassignment already marks the row dirty
    try:
        flag_modified(tenant, "features_enabled")  # belt-and-suspenders for JSONB
    except Exception:  # noqa: BLE001 — only fails on non-ORM objects (tests)
        pass
    # Drop the per-instance memo so the current run sees the new domain.
    if hasattr(tenant, "_internal_domains_cache"):
        try:
            delattr(tenant, "_internal_domains_cache")
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "Learned internal domain %r for tenant %s from outbound email",
        dom, tenant.id,
    )


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


def _upsert_contact(
    session: Session,
    tenant_id: uuid.UUID,
    address: str,
    cache: Optional[Dict[str, Contact]] = None,
) -> Optional[Contact]:
    if not address:
        return None
    if cache is not None and address in cache:
        return cache[address]
    contact = (
        session.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.email == address)
        .first()
    )
    if contact is None:
        contact = Contact(tenant_id=tenant_id, email=address, name=address.split("@")[0])
        session.add(contact)
        session.flush()
    if cache is not None:
        cache[address] = contact
    return contact


def _upsert_conversation(
    session: Session,
    tenant_id: uuid.UUID,
    thread_key: str,
    subject: Optional[str],
    classification: str,
    contact_id: Optional[uuid.UUID],
    received_at: Optional[datetime],
    cache: Optional[Dict[str, Conversation]] = None,
) -> Conversation:
    conv = cache.get(thread_key) if cache is not None else None
    if conv is None:
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
    if cache is not None:
        cache[thread_key] = conv
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
    caches: Optional[IngestCaches] = None,
) -> Optional[uuid.UUID]:
    """Process a single email.  Returns the Interaction id, or None if filtered.

    Idempotent on ``provider_message_id`` + RFC-822 Message-ID so the
    poller can re-run a window without creating duplicates.

    ``caches`` (optional) reuses contact/conversation lookups across
    messages for bulk callers — see :class:`IngestCaches`.
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

    # Before classifying, learn this tenant's own domain from outbound mail
    # so classification is correct even for login-less (API-key) tenants.
    _learn_internal_domain_from_outbound(session, tenant, email)

    # Prompt-variant routing (A/B) — same selection API the analysis path
    # uses (tasks.py), just the sync flavor since ingest runs off a sync
    # Celery-worker session. Recorded on the Interaction below (once we
    # know we're keeping this email) so the LLM judge can attribute its
    # score to the right bucket instead of always writing
    # prompt_variant_id=None for this surface.
    variant = select_variant_sync(
        session,
        tenant,
        surface="email_classifier",
        fallback_template=_CLASSIFIER_FALLBACK_PROMPT,
    )

    verdict = await classifier.classify(
        EmailForClassification(
            subject=email.subject,
            from_address=email.from_address,
            to_addresses=email.to_addresses,
            cc_addresses=email.cc_addresses,
            body_preview=email.body_text,
            headers=email.headers,
            tenant_domains=_tenant_domains(tenant, session),
        ),
        system_prompt_override=variant.prompt_template,
    )

    if not verdict.is_external:
        # DSNs (mailer-daemon / undeliverable) are auto-generated and land
        # here — but a bounce for a tracked outreach send must still halt
        # the member and notify the consumer, even though it never becomes
        # an Interaction.
        if email.direction == "inbound":
            try:
                handled = outreach_replies.handle_possible_bounce(
                    session,
                    tenant.id,
                    from_address=email.from_address,
                    subject=email.subject,
                    body_text=email.body_text,
                    in_reply_to=email.in_reply_to,
                    references=list(email.references or []),
                )
                if handled:
                    session.flush()
            except Exception:
                logger.warning("outreach bounce hook failed", exc_info=True)
        logger.info(
            "Skipping internal email msgid=%s reason=%s",
            email.message_id, verdict.reason,
        )
        return None

    counterparty = _counterparty_address(email)
    contact = _upsert_contact(
        session, tenant.id, counterparty,
        cache=caches.contacts if caches is not None else None,
    )
    agent = _resolve_agent(session, tenant.id, email.agent_email)

    # Campaign attribution: if this is an inbound reply to a tracked
    # campaign send, link it and record a reply event so campaign
    # analytics stay fresh without a separate pass. Matching covers
    # In-Reply-To, the whole References chain, and (for outreach
    # campaigns, where Outlook sends never expose their Message-ID) a
    # sender-address fallback — see find_recipient_for_reply.
    campaign_id = None
    campaign_recipient = None
    if email.direction == "inbound":
        campaign_recipient = outreach_replies.find_recipient_for_reply(
            session,
            tenant.id,
            in_reply_to=email.in_reply_to,
            references=list(email.references or []),
            from_address=email.from_address,
        )
        if campaign_recipient is not None:
            campaign_id = campaign_recipient.campaign_id
            session.add(
                CampaignEvent(
                    campaign_id=campaign_recipient.campaign_id,
                    tenant_id=tenant.id,
                    recipient_id=campaign_recipient.id,
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
        cache=caches.conversations if caches is not None else None,
    )

    interaction = Interaction(
        tenant_id=tenant.id,
        agent_id=agent.id if agent else None,
        contact_id=contact.id if contact else None,
        conversation_id=conversation.id,
        campaign_id=campaign_id,
        channel="email",
        bcc_addresses=list(email.bcc_addresses),
        body_html=email.body_html,
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
        insights=merge_variant_insight(None, "email_classifier", variant.variant_id),
    )
    # created_at is "when the interaction happened" everywhere it's
    # consumed (timelines, per-contact latest, analytics windows), so an
    # email must carry its Date header, not the ingest time — otherwise a
    # backfilled 50-message thread renders as 50 emails at one identical
    # timestamp. Unparseable Date falls through to the server default.
    if email.received_at is not None:
        interaction.created_at = email.received_at
    session.add(interaction)
    session.flush()

    # Outreach lifecycle: a reply attributed to an outreach campaign halts
    # the member's sequence, flips the prospect's pipeline status, honors
    # opt-out replies, and emits the outreach webhooks. External-kind
    # campaigns skip this (the handler checks) — they only get the
    # CampaignEvent recorded above.
    if campaign_recipient is not None:
        try:
            outreach_replies.handle_outreach_reply(
                session, tenant.id, interaction, campaign_recipient, contact
            )
        except Exception:
            logger.warning("outreach reply hook failed", exc_info=True)

    # Persist attachments.  Bytes go to S3 if the store is configured;
    # we always write the row so the UI can show "customer attached X".
    store = get_store()
    for att in email.attachments or []:
        data = att.data
        if data is None and email.attachment_fetcher is not None:
            try:
                data = email.attachment_fetcher(att)
            except Exception:
                logger.exception(
                    "Attachment fetcher raised (non-fatal) msg=%s filename=%s",
                    email.message_id, att.filename,
                )
                data = None

        s3_key: Optional[str] = None
        size_bytes = att.size_bytes
        if data is not None:
            size_bytes = len(data)
            s3_key = store.put(
                tenant_id=tenant.id,
                interaction_id=interaction.id,
                filename=att.filename,
                content_type=att.content_type,
                data=data,
            )

        session.add(InteractionAttachment(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
            filename=att.filename,
            content_type=att.content_type,
            size_bytes=size_bytes,
            s3_key=s3_key,
            provider_attachment_id=att.provider_attachment_id,
            direction=email.direction,
            inline=att.inline,
            content_id=att.content_id,
        ))
    session.flush()

    # Enqueue analysis — Celery is optional in dev/test environments.
    try:
        from backend.app.tasks import process_text_interaction

        process_text_interaction.delay(str(interaction.id))
    except Exception:  # pragma: no cover
        logger.exception("Failed to enqueue text pipeline; analysis deferred")

    # Inbound emails get a parallel pass through the Action Plan
    # matcher so a reply to a sent step lands a StepResponse + Call D
    # extraction without waiting on the full analysis pipeline.
    # Outbound emails skip this hook — they're tied to a step via the
    # explicit POST /sent endpoint, not via the inbound matcher.
    if email.direction == "inbound":
        try:
            from backend.app.tasks import action_plan_match_inbound_email

            action_plan_match_inbound_email.delay(str(interaction.id))
        except Exception:
            logger.exception(
                "Failed to enqueue action_plan_match_inbound_email "
                "(non-fatal)"
            )

    return interaction.id
