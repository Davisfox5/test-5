"""SQLAlchemy ORM models — every table in the LINDA schema."""

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
    UniqueConstraint,
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
    # Days to keep ``CallRecording`` audio before the nightly cleanup
    # deletes the S3 object + row. 0 means "keep forever" (not
    # recommended outside of short-lived test tenants). Regulated
    # industries typically set 2555 (7 years).
    recording_retention_days: Mapped[int] = mapped_column(Integer, default=0)
    enrichment_pdl_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enrichment_apollo_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    keyterm_boost_list: Mapped[list] = mapped_column(JSONB, default=list)
    question_keyterms: Mapped[list] = mapped_column(JSONB, default=list)
    default_language: Mapped[str] = mapped_column(String, default="en")
    translation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    features_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Outcomes webhook HMAC secret verified on X-Linda-Signature.
    outcomes_hmac_secret: Mapped[Optional[str]] = mapped_column(String)
    # How many hours of call audio to retain after processing.
    audio_retention_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")
    # Subscription seat limits (admin floor 1). Total seat_limit includes the admin(s).
    seat_limit: Mapped[int] = mapped_column(Integer, default=1)
    admin_seat_limit: Mapped[int] = mapped_column(Integer, default=1)
    # Tier key from backend.app.services.subscription_tiers.SUBSCRIPTION_TIERS.
    subscription_tier: Mapped[str] = mapped_column(String, default="solo")
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String)
    # True while a tier downgrade has left the tenant over-headcount.
    pending_seat_reconciliation: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-tenant operating brief consumed by orchestrator + agents.
    tenant_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_white_label: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # ── Plan + trial (Tier 1/2/3 customer-facing) ──────────
    # plan_tier: sandbox | starter | growth | enterprise
    plan_tier: Mapped[str] = mapped_column(String, nullable=False, default="sandbox", server_default="sandbox")
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
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
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # bcrypt hash (60 chars). Null for Clerk-JWT accounts or pre-password users.
    password_hash: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When non-NULL, the user was suspended for a system reason (e.g. tier_downgrade).
    suspension_reason: Mapped[Optional[str]] = mapped_column(String)
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


class CallRecording(Base):
    """Audio recording of a voice interaction.

    Populated by the Twilio ``recordingStatusCallback`` handler (and the
    equivalent providers). We mirror the raw audio into our own S3 bucket
    so it's retained under the tenant's control even if they revoke the
    provider integration.
    """

    __tablename__ = "call_recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL"), index=True
    )
    live_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("live_sessions.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)  # twilio|signalwire|telnyx
    provider_recording_id: Mapped[Optional[str]] = mapped_column(String)
    s3_key: Mapped[Optional[str]] = mapped_column(String)
    content_type: Mapped[str] = mapped_column(String, default="audio/wav")
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    # pending | stored | failed
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    stored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


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
# INTERACTIONS (omnichannel — voice, email, chat)
#
# SMS and WhatsApp rows may exist from earlier backfills; they remain
# readable but no new rows with those channels are created.
# ──────────────────────────────────────────────────────────


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )

    # Type and source — voice|email|chat (sms/whatsapp stubbed)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String)
    direction: Mapped[Optional[str]] = mapped_column(String)  # inbound|outbound|internal

    # Which PromptVariant produced the most recent .insights (for A/B + rollback).
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    # Content
    title: Mapped[Optional[str]] = mapped_column(String)
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    thread_id: Mapped[Optional[str]] = mapped_column(String)

    # Audio-specific
    audio_s3_key: Mapped[Optional[str]] = mapped_column(String)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    caller_phone: Mapped[Optional[str]] = mapped_column(String)

    # Email-specific (populated for channel='email')
    from_address: Mapped[Optional[str]] = mapped_column(String)
    to_addresses: Mapped[list] = mapped_column(JSONB, default=list)
    cc_addresses: Mapped[list] = mapped_column(JSONB, default=list)
    bcc_addresses: Mapped[list] = mapped_column(JSONB, default=list)
    subject: Mapped[Optional[str]] = mapped_column(String)
    message_id: Mapped[Optional[str]] = mapped_column(String, unique=True)  # RFC-822
    in_reply_to: Mapped[Optional[str]] = mapped_column(String)
    references: Mapped[list] = mapped_column(JSONB, default=list)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String)  # Gmail/Graph id
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[Optional[str]] = mapped_column(String)  # sales|support|it|other
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float)
    body_html: Mapped[Optional[str]] = mapped_column(Text)  # sanitized, optional

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


# ──────────────────────────────────────────────────────────
# INTERACTION ATTACHMENTS
#
# One row per file on an email (inbound or outbound). The actual bytes
# live in S3 when AWS_S3_BUCKET is configured — we only keep metadata +
# s3_key in Postgres.
# ──────────────────────────────────────────────────────────


class InteractionAttachment(Base):
    __tablename__ = "interaction_attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    interaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interactions.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    filename: Mapped[Optional[str]] = mapped_column(String)
    content_type: Mapped[Optional[str]] = mapped_column(String)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    s3_key: Mapped[Optional[str]] = mapped_column(String)
    provider_attachment_id: Mapped[Optional[str]] = mapped_column(String)
    direction: Mapped[Optional[str]] = mapped_column(String)
    inline: Mapped[bool] = mapped_column(Boolean, default=False)
    content_id: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# SCORING & ORCHESTRATOR (see docs/SCORING_ARCHITECTURE.md)
# ──────────────────────────────────────────────────────────


class InteractionFeatures(Base):
    """Canonical per-interaction feature store.

    Everything computed from the transcript — deterministic metrics,
    parsed LLM output, proxy-outcome events — lives here.  Scoring and
    orchestrator code reads only from this row, never from the raw
    ``interactions.insights`` blob.
    """

    __tablename__ = "interaction_features"

    interaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interactions.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    deterministic: Mapped[dict] = mapped_column(JSONB, default=dict)
    llm_structured: Mapped[dict] = mapped_column(JSONB, default=dict)
    embeddings_ref: Mapped[Optional[str]] = mapped_column(Text)
    proxy_outcomes: Mapped[dict] = mapped_column(JSONB, default=dict)
    scorer_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaReport(Base):
    """Per-interaction structured delta consumed by the orchestrator."""

    __tablename__ = "delta_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    delta: Mapped[dict] = mapped_column(JSONB, default=dict)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClientProfile(Base):
    __tablename__ = "client_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ManagerProfile(Base):
    __tablename__ = "manager_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    manager_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BusinessProfile(Base):
    """Tenant-level rollup profile (the 'business' is the tenant firm)."""

    __tablename__ = "business_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    business_tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    top_factors: Mapped[list] = mapped_column(JSONB, default=list)
    source_event: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScorerVersion(Base):
    """A named calibration/weight snapshot for one scorer.

    Note: ``ScorerVersion`` (this file) versions *output-side* calibration
    — the Platt / Cox / IRT weights that turn raw LLM outputs and
    deterministic features into calibrated scores.  It is distinct from
    ``PromptVariant`` below, which versions *input-side* prompts sent to
    Claude.  Both coexist: ``Interaction.prompt_variant_id`` records which
    prompt produced the insights blob; ``InteractionFeatures.scorer_versions``
    records which scoring weights applied on top.
    """

    __tablename__ = "scorer_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id"))
    scorer_name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    calibration: Mapped[dict] = mapped_column(JSONB, default=dict)
    ece: Mapped[Optional[float]] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CorrectionEvent(Base):
    """Active-learning correction submitted by a user.

    Distinct from ``FeedbackEvent`` below: a ``CorrectionEvent`` is a
    targeted replacement of a specific scorer's output (e.g. user marks
    "this churn_risk is wrong — it should be low").  ``FeedbackEvent``
    is the generic implicit/explicit signal stream ingested from the
    product surfaces (thumbs, copy-edit, retry, accept).
    """

    __tablename__ = "correction_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String)
    original: Mapped[dict] = mapped_column(JSONB, default=dict)
    correction: Mapped[dict] = mapped_column(JSONB, default=dict)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# CONVERSATIONS (threading across email / voice / chat)
# Also used by Ask Linda for chat conversations + write proposals.
# ──────────────────────────────────────────────────────────


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    channel: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String)
    thread_key: Mapped[Optional[str]] = mapped_column(String, index=True)
    classification: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# ASK LINDA — chat conversations, messages, write proposals
# Separate namespace from the email/voice Conversation above.
# ──────────────────────────────────────────────────────────


class LindaChatConversation(Base):
    __tablename__ = "linda_chat_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)


# ──────────────────────────────────────────────────────────
# EMAIL INGESTION CURSOR
# ──────────────────────────────────────────────────────────


class EmailSyncCursor(Base):
    __tablename__ = "email_sync_cursors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), unique=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)
    history_id: Mapped[Optional[str]] = mapped_column(String)
    delta_link: Mapped[Optional[str]] = mapped_column(Text)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# MARKETING CAMPAIGNS
# ──────────────────────────────────────────────────────────


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String)
    external_id: Mapped[Optional[str]] = mapped_column(String)
    subject: Mapped[Optional[str]] = mapped_column(String)
    variant: Mapped[Optional[str]] = mapped_column(String)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    email_address: Mapped[Optional[str]] = mapped_column(String)
    external_message_id: Mapped[Optional[str]] = mapped_column(String)
    rfc822_message_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class CampaignEvent(Base):
    __tablename__ = "campaign_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    recipient_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("campaign_recipients.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


# ──────────────────────────────────────────────────────────
# CONTINUOUS AI IMPROVEMENT
# ──────────────────────────────────────────────────────────


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    action_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("action_items.id"))
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    surface: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    signal_type: Mapped[str] = mapped_column(String, nullable=False)
    insight_dimension: Mapped[Optional[str]] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TranscriptCorrection(Base):
    __tablename__ = "transcript_corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_at_correction: Mapped[Optional[float]] = mapped_column(Float)
    corrected_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    correction_source: Mapped[str] = mapped_column(String, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InsightQualityScore(Base):
    __tablename__ = "insight_quality_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    surface: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_type: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_id: Mapped[str] = mapped_column(String, nullable=False)
    dimension: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PromptVariant(Base):
    """Versioned LLM prompt template, A/B-routable across production surfaces.

    See the note on ``ScorerVersion`` for how these two versioning systems
    relate: prompt variants govern the *input* to Claude, scorer versions
    govern the *output*-side calibration.
    """

    __tablename__ = "prompt_variants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    target_surface: Mapped[str] = mapped_column(String, nullable=False)
    target_tier: Mapped[Optional[str]] = mapped_column(String)
    target_channel: Mapped[Optional[str]] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="draft")
    parent_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TenantPromptConfig(Base):
    __tablename__ = "tenant_prompt_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), unique=True)
    active_prompt_variant_ids: Mapped[dict] = mapped_column(JSONB, default=dict)
    few_shot_pool: Mapped[dict] = mapped_column(JSONB, default=dict)
    persona_block: Mapped[Optional[str]] = mapped_column(Text)
    acronyms: Mapped[dict] = mapped_column(JSONB, default=dict)
    custom_terms: Mapped[list] = mapped_column(JSONB, default=list)
    rag_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    parameter_overrides: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))


class VocabularyCandidate(Base):
    __tablename__ = "vocabulary_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    term: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[str] = mapped_column(String, default="medium")
    source: Mapped[Optional[str]] = mapped_column(String)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending")
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvaluationReferenceSet(Base):
    __tablename__ = "evaluation_reference_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id"))
    surface: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    interaction_ids: Mapped[list] = mapped_column(JSONB, default=list)
    reference_outputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    surface: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    hypothesis: Mapped[Optional[str]] = mapped_column(Text)
    control_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    treatment_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    conclusion: Mapped[Optional[str]] = mapped_column(Text)
    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WerMetric(Base):
    __tablename__ = "wer_metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    asr_engine: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[Optional[str]] = mapped_column(String)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    word_error_rate: Mapped[float] = mapped_column(Float, default=0.0)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CrossTenantAnalytic(Base):
    """Cross-tenant aggregate metric — no ``tenant_id`` column by design."""

    __tablename__ = "cross_tenant_analytics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    metric_name: Mapped[str] = mapped_column(String, nullable=False)
    bucket: Mapped[Optional[str]] = mapped_column(String)
    surface: Mapped[Optional[str]] = mapped_column(String)
    channel: Mapped[Optional[str]] = mapped_column(String)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[Optional[float]] = mapped_column(Float)
    distribution: Mapped[dict] = mapped_column(JSONB, default=dict)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ──────────────────────────────────────────────────────────
# OUTCOME EVENT INGESTION (idempotency + dead-letter)
# ──────────────────────────────────────────────────────────


class OutcomeEventIngestion(Base):
    """Idempotency record for externally-posted outcome events.

    Uniquely keyed on ``(tenant_id, event_id)`` — a repeated webhook
    delivery with the same ``event_id`` is 200-accepted but does not
    re-apply the event to ``InteractionFeatures.proxy_outcomes``.
    """

    __tablename__ = "outcome_event_ingestions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "event_id", name="uq_outcome_ingestion_event"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    outcome_type: Mapped[str] = mapped_column(String, nullable=False)
    interaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DroppedOutcomeEvent(Base):
    """Dead-letter log for outcome payloads that failed validation.

    Captured reasons include: ``unknown_outcome_type``, ``future_timestamp``,
    ``hmac_signature_invalid``, ``interaction_not_found``, ``schema_error``.
    """

    __tablename__ = "dropped_outcome_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id"))
    reason: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    headers_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LindaChatMessage(Base):
    """Every turn in a Linda chat — user message, assistant reply, or tool result."""

    __tablename__ = "linda_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linda_chat_conversations.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String, nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[Optional[list]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WriteProposal(Base):
    """A draft write Linda proposed. Only dispatches after explicit user confirm."""

    __tablename__ = "write_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linda_chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", server_default="pending")
    resulting_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# PUBLIC DEMO — email capture (pre-signup leads)
# ──────────────────────────────────────────────────────────


class DemoEmailCapture(Base):
    """Emails collected from the public demo's 60-second gate. Pre-tenant."""

    __tablename__ = "demo_email_captures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source: Mapped[Optional[str]] = mapped_column(String)
    utm: Mapped[dict] = mapped_column(JSONB, default=dict)
    converted_tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
