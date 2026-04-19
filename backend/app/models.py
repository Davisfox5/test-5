"""SQLAlchemy ORM models — every table in the CallSight schema."""

import uuid
from datetime import date, datetime
from typing import Any, List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ──────────────────────────────────────────────────────────
# TENANTS
# ──────────────────────────────────────────────────────────


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    branding_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    transcription_engine: Mapped[str] = mapped_column(String, default="deepgram")
    automation_level: Mapped[str] = mapped_column(String, default="approval")
    pii_redaction_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pii_redaction_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    audio_storage_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enrichment_pdl_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enrichment_apollo_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    keyterm_boost_list: Mapped[list] = mapped_column(JSONB, default=list)
    question_keyterms: Mapped[list] = mapped_column(JSONB, default=list)
    default_language: Mapped[str] = mapped_column(String, default="en")
    translation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    features_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Subscription seat limits. The admin floor is 1 (min viable org).
    # Total seat_limit includes the admin(s). Upserted from the billing
    # system; enforced when creating new User rows.
    seat_limit: Mapped[int] = mapped_column(Integer, default=1)
    admin_seat_limit: Mapped[int] = mapped_column(Integer, default=1)
    # Tier key from backend.app.services.subscription_tiers.SUBSCRIPTION_TIERS.
    # Seat + admin limits + feature defaults are derived from this on every
    # change via ``apply_tier``.
    subscription_tier: Mapped[str] = mapped_column(String, default="solo")
    # LINDA's per-tenant operating brief — everything the orchestrator and its
    # agents should know about this tenant. Assembled by the ContextBuilder
    # agent from KB docs, onboarding-interview answers, explicit overrides,
    # and the outcomes-loop refiner. Injected into analyze/coaching prompts.
    tenant_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    users: Mapped[List["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    api_keys: Mapped[List["ApiKey"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


# ──────────────────────────────────────────────────────────
# USERS
# ──────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String)
    # agent | manager | admin. Admins can manage users, tenant settings,
    # integrations, webhooks; managers can monitor calls + approve most
    # things agents can't; agents are the call-handling role.
    role: Mapped[str] = mapped_column(String, default="agent")
    # bcrypt hash (60 chars). Null for accounts that authenticate via Clerk
    # JWT or haven't set a local password yet.
    password_hash: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="users")


# ──────────────────────────────────────────────────────────
# API KEYS
# ──────────────────────────────────────────────────────────


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String)
    scopes: Mapped[list] = mapped_column(JSONB, default=lambda: ["read:all", "write:all"])
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


# ──────────────────────────────────────────────────────────
# WEBHOOKS (outbound to MSP systems)
# ──────────────────────────────────────────────────────────


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String, nullable=False)
    events: Mapped[list] = mapped_column(JSONB, default=lambda: ["*"])
    secret: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Counters maintained by the dispatcher so the admin UI can show health
    # without scanning deliveries every page load.
    last_delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookDelivery(Base):
    """One row per delivery attempt sequence. Used for retry tracking + audit.

    A single event creates one WebhookDelivery per matching webhook. The row
    gets updated in place as retries fire; the attempts list holds the per-
    retry metadata (status_code, error, timestamp).
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    event: Mapped[str] = mapped_column(String, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # pending | sent | failed | dead_letter
    status: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[list] = mapped_column(JSONB, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CUSTOMERS (tenants' own customers — CRM-style accounts)
# ──────────────────────────────────────────────────────────


class Customer(Base):
    """A customer of the tenant — i.e., a CRM-style account the tenant sells
    to or supports. The tenant itself is ``Tenant``; this model represents
    the *end customers* whose contacts appear on calls."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String)
    crm_id: Mapped[Optional[str]] = mapped_column(String)
    industry: Mapped[Optional[str]] = mapped_column(String)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    # Per-customer dossier, built by CustomerBriefBuilder. Readable by
    # LINDA's agents at call time to ground live coaching in what we know
    # about this specific customer.
    customer_brief: Mapped[dict] = mapped_column(JSONB, default=dict)


# ──────────────────────────────────────────────────────────
# CUSTOMER OUTCOME EVENTS (lifecycle signals)
# ──────────────────────────────────────────────────────────


class EmailSend(Base):
    """Outbound email delivery record — audit + dedupe.

    Populated when an agent/admin sends a follow-up email via the stored
    Gmail/Outlook OAuth token. One row per attempt; failed deliveries are
    kept (``status='failed'``) so the UI can show a retry button.
    """

    __tablename__ = "email_sends"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    sender_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)  # google | microsoft
    to_address: Mapped[str] = mapped_column(String, nullable=False)
    cc_address: Mapped[Optional[str]] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | sent | failed
    status: Mapped[str] = mapped_column(String, default="pending")
    provider_message_id: Mapped[Optional[str]] = mapped_column(String)
    error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CustomerOutcomeEvent(Base):
    """A lifecycle event on a Customer — the signal we learn from.

    Emitted by the AI analysis pipeline (when insights indicate churn/upsell
    triggers), by webhook sync from a CRM, or manually via the outcome API.
    The CustomerBriefBuilder reads these to populate the "what's worked, what's
    failed" section of each customer's brief, and the TenantBriefRefiner
    aggregates them across customers to learn tenant-wide patterns.
    """

    __tablename__ = "customer_outcome_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    # Enum-ish string. Supported values:
    #   became_customer | upsold | renewed | churned | satisfaction_change
    #   | escalation | advocate_signal | at_risk_flagged
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    magnitude: Mapped[Optional[float]] = mapped_column(Float)
    signal_strength: Mapped[Optional[float]] = mapped_column(Float)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String)  # ai_inferred|agent_logged|crm_sync
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CONTACTS
# ──────────────────────────────────────────────────────────


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[Optional[str]] = mapped_column(String)
    email: Mapped[Optional[str]] = mapped_column(String)
    phone: Mapped[Optional[str]] = mapped_column(String)
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("customers.id"))
    crm_id: Mapped[Optional[str]] = mapped_column(String)
    crm_source: Mapped[Optional[str]] = mapped_column(String)
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sentiment_trend: Mapped[list] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    customer: Mapped[Optional["Customer"]] = relationship()


# ──────────────────────────────────────────────────────────
# INTERACTIONS (omnichannel — voice, sms, email, chat, whatsapp)
# ──────────────────────────────────────────────────────────


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))

    # Type and source
    channel: Mapped[str] = mapped_column(String, nullable=False)  # voice|sms|email|chat|whatsapp
    source: Mapped[Optional[str]] = mapped_column(String)
    direction: Mapped[Optional[str]] = mapped_column(String)  # inbound|outbound|internal

    # Content
    title: Mapped[Optional[str]] = mapped_column(String)
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    thread_id: Mapped[Optional[str]] = mapped_column(String)

    # Audio-specific
    audio_s3_key: Mapped[Optional[str]] = mapped_column(String)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    caller_phone: Mapped[Optional[str]] = mapped_column(String)

    # Processing
    status: Mapped[str] = mapped_column(String, default="processing")
    engine: Mapped[str] = mapped_column(String, default="deepgram")
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    pii_redacted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Call metrics (computed pre-LLM)
    call_metrics: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Tiered AI analysis
    complexity_score: Mapped[Optional[float]] = mapped_column(Float)
    analysis_tier: Mapped[Optional[str]] = mapped_column(String)

    # Translation
    transcript_translated: Mapped[Optional[list]] = mapped_column(JSONB)
    detected_language: Mapped[Optional[str]] = mapped_column(String)

    participants: Mapped[list] = mapped_column(JSONB, default=list)

    # Call-level outcome. Populated either by the AI analysis pass (from
    # insights.churn_risk / action_items / dispositions) or by the agent via
    # POST /interactions/{id}/outcome. Feeds both the TenantBriefRefiner
    # (what works / doesn't) and the CustomerBriefBuilder (per-customer wins).
    outcome_type: Mapped[Optional[str]] = mapped_column(String)
    outcome_value: Mapped[Optional[float]] = mapped_column(Float)
    outcome_confidence: Mapped[Optional[float]] = mapped_column(Float)
    outcome_source: Mapped[Optional[str]] = mapped_column(String)
    outcome_notes: Mapped[Optional[str]] = mapped_column(Text)
    outcome_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    agent: Mapped[Optional["User"]] = relationship()
    contact: Mapped[Optional["Contact"]] = relationship()
    action_items: Mapped[List["ActionItem"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")
    scores: Mapped[List["InteractionScore"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")
    snippets: Mapped[List["InteractionSnippet"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")
    comments: Mapped[List["InteractionComment"]] = relationship(back_populates="interaction", cascade="all, delete-orphan")


# ──────────────────────────────────────────────────────────
# ACTION ITEMS
# ──────────────────────────────────────────────────────────


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String)
    priority: Mapped[str] = mapped_column(String, default="medium")
    status: Mapped[str] = mapped_column(String, default="pending")
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String)
    email_draft: Mapped[Optional[dict]] = mapped_column(JSONB)
    automation_status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="action_items")


# ──────────────────────────────────────────────────────────
# SCORECARDS
# ──────────────────────────────────────────────────────────


class ScorecardTemplate(Base):
    __tablename__ = "scorecard_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    criteria: Mapped[list] = mapped_column(JSONB, nullable=False)
    channel_filter: Mapped[Optional[list]] = mapped_column(JSONB)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InteractionScore(Base):
    __tablename__ = "interaction_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scorecard_templates.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    total_score: Mapped[Optional[float]] = mapped_column(Float)
    criterion_scores: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="scores")


# ──────────────────────────────────────────────────────────
# SNIPPETS / CALL LIBRARY
# ──────────────────────────────────────────────────────────


class InteractionSnippet(Base):
    __tablename__ = "interaction_snippets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    snippet_type: Mapped[Optional[str]] = mapped_column(String)
    quality: Mapped[Optional[str]] = mapped_column(String)
    title: Mapped[Optional[str]] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text)
    transcript_excerpt: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    in_library: Mapped[bool] = mapped_column(Boolean, default=False)
    library_category: Mapped[Optional[str]] = mapped_column(String)
    promoted_by: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="snippets")


# ──────────────────────────────────────────────────────────
# COMMENTS / ASYNC REVIEW
# ──────────────────────────────────────────────────────────


class InteractionComment(Base):
    __tablename__ = "interaction_comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    timestamp_sec: Mapped[Optional[float]] = mapped_column(Float)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interaction: Mapped[Interaction] = relationship(back_populates="comments")


# ──────────────────────────────────────────────────────────
# LIVE SESSIONS
# ──────────────────────────────────────────────────────────


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("interactions.id"))
    source: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    transcript_buffer: Mapped[list] = mapped_column(JSONB, default=list)
    coaching_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# ONBOARDING INTERVIEW (one per tenant; may be resumed)
# ──────────────────────────────────────────────────────────


class CustomerNote(Base):
    """A free-form note an agent attaches to a customer during or after a call.

    Notes are durable evidence that the CustomerBriefBuilder reads on its next
    run — they let agents capture observations LINDA might have missed in the
    transcript (e.g., "Sarah mentioned she's leaving Acme next month"). Once
    the builder has folded a note's content into the brief we mark it
    ``reviewed_at``; unreviewed notes are the fresh-evidence pile.
    """

    __tablename__ = "customer_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    author_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TenantBriefSuggestion(Base):
    """A proposed update to a tenant's onboarding-owned brief section.

    Generated by the Infer-From-Sources agent. Stays ``pending`` until an
    admin approves or rejects it. On approve we splice it into
    ``Tenant.tenant_context`` (treating the suggestion like an onboarding
    answer); on reject it's archived for audit.
    """

    __tablename__ = "tenant_brief_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Which section of the brief this proposes changes to:
    # goals | kpis | strategies | org_structure | personal_touches
    section: Mapped[str] = mapped_column(String, nullable=False)
    # Dotted key inside the section (e.g. "personal_touches.greeting_style").
    # Empty for list-append suggestions on list-typed sections.
    path: Mapped[Optional[str]] = mapped_column(String)
    # The proposed value — can be a string, list, or dict depending on section.
    proposed_value: Mapped[Any] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    # Identifiers of the source rows (interactions, events) used so an admin
    # can click through to the evidence.
    evidence_refs: Mapped[list] = mapped_column(JSONB, default=list)
    # pending | approved | rejected | superseded
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))


class OnboardingSession(Base):
    """Persistent state for a LINDA onboarding interview.

    One row per tenant (we reuse the latest non-abandoned row so admins can
    resume). State is the full ``OnboardingInterview`` state dict.
    """

    __tablename__ = "onboarding_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    started_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    # active | completed | abandoned
    status: Mapped[str] = mapped_column(String, default="active")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# KNOWLEDGE BASE
# ──────────────────────────────────────────────────────────


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    title: Mapped[Optional[str]] = mapped_column(String)
    content: Mapped[Optional[str]] = mapped_column(Text)
    content_hash: Mapped[Optional[str]] = mapped_column(String)
    source_type: Mapped[Optional[str]] = mapped_column(String)
    source_url: Mapped[Optional[str]] = mapped_column(String)
    source_external_id: Mapped[Optional[str]] = mapped_column(String)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    qdrant_point_id: Mapped[Optional[str]] = mapped_column(String)
    embedded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KBChunk(Base):
    """One embedded slice of a KB document.

    For the pgvector backend, ``embedding`` stores the vector directly. For the
    Qdrant backend, the point lives in Qdrant and this row is just metadata.
    """

    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True)
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    content_hash: Mapped[Optional[str]] = mapped_column(String)
    # pgvector column is added by the migration (sqlalchemy doesn't ship a vector type).
    # We access it via raw SQL in PgVectorStore.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PinnedKBCard(Base):
    """An agent pin on a KB chunk for a particular contact.

    While pinned, the retrieval pipeline will still surface the chunk in search
    results but will suppress re-triggering a new card for it within a session
    or subsequent calls with the same contact.
    """

    __tablename__ = "pinned_kb_cards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"))
    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_chunks.id", ondelete="CASCADE"))
    pinned_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# OAUTH INTEGRATIONS
# ──────────────────────────────────────────────────────────


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(Text)  # AES-256 encrypted
    refresh_token: Mapped[Optional[str]] = mapped_column(Text)  # AES-256 encrypted
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    # Provider-specific config (Salesforce instance URL, HubSpot portal id,
    # custom property mappings, etc.). Freeform JSONB so each adapter can
    # stash what it needs without a model change.
    provider_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrmSyncLog(Base):
    """One row per CRM sync run. Used by the admin UI to show sync status."""

    __tablename__ = "crm_sync_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # running | success | partial | failed
    status: Mapped[str] = mapped_column(String, default="running")
    customers_upserted: Mapped[int] = mapped_column(Integer, default=0)
    contacts_upserted: Mapped[int] = mapped_column(Integer, default=0)
    briefs_rebuilt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# TENANT INSIGHTS (scheduled Opus analysis)
# ──────────────────────────────────────────────────────────


class TenantInsight(Base):
    __tablename__ = "tenant_insights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[Optional[date]] = mapped_column(Date)
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
