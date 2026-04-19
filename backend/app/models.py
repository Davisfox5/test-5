"""SQLAlchemy ORM models — every table in the CallSight schema."""

import uuid
from datetime import date, datetime
from typing import List, Optional

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
    default_language: Mapped[str] = mapped_column(String, default="en")
    translation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    features_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
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
    role: Mapped[str] = mapped_column(String, default="agent")
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# COMPANIES
# ──────────────────────────────────────────────────────────


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String)
    crm_id: Mapped[Optional[str]] = mapped_column(String)
    industry: Mapped[Optional[str]] = mapped_column(String)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


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
    company_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("companies.id"))
    crm_id: Mapped[Optional[str]] = mapped_column(String)
    crm_source: Mapped[Optional[str]] = mapped_column(String)
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sentiment_trend: Mapped[list] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped[Optional["Company"]] = relationship()


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
    subject: Mapped[Optional[str]] = mapped_column(String)
    message_id: Mapped[Optional[str]] = mapped_column(String, unique=True)  # RFC-822
    in_reply_to: Mapped[Optional[str]] = mapped_column(String)
    references: Mapped[list] = mapped_column(JSONB, default=list)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String)  # Gmail/Graph id
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[Optional[str]] = mapped_column(String)  # sales|support|it|other
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float)

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
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
# CONVERSATIONS (threading across email / voice / chat)
# ──────────────────────────────────────────────────────────


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    channel: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String)
    thread_key: Mapped[Optional[str]] = mapped_column(String, index=True)  # RFC-822 root id or hash
    classification: Mapped[Optional[str]] = mapped_column(String)  # sales|support|it|other
    status: Mapped[str] = mapped_column(String, default="open")  # open|waiting_customer|waiting_agent|closed
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Active classifier/reply-drafter variant for the thread — same variant for
    # the whole conversation so the user's experience is consistent within an
    # A/B test window.
    prompt_variant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ──────────────────────────────────────────────────────────
# EMAIL INGESTION CURSOR (per-integration sync state)
# ──────────────────────────────────────────────────────────


class EmailSyncCursor(Base):
    __tablename__ = "email_sync_cursors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), unique=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)  # google|microsoft
    history_id: Mapped[Optional[str]] = mapped_column(String)       # Gmail historyId
    delta_link: Mapped[Optional[str]] = mapped_column(Text)         # Graph deltaLink
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ──────────────────────────────────────────────────────────
# MARKETING CAMPAIGNS
#
# We don't generate campaigns — we monitor them.  External ESPs (Mailchimp,
# HubSpot, Klaviyo, SendGrid, etc.) push metadata + engagement events in,
# and we attribute replies / downstream interactions back so AI analysis
# can correlate campaign framing with customer sentiment.
# ──────────────────────────────────────────────────────────


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)  # email|sms|push|other
    provider: Mapped[Optional[str]] = mapped_column(String)  # mailchimp|hubspot|klaviyo|sendgrid|custom
    external_id: Mapped[Optional[str]] = mapped_column(String)  # ESP's campaign id
    subject: Mapped[Optional[str]] = mapped_column(String)
    variant: Mapped[Optional[str]] = mapped_column(String)  # A/B label
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    insights: Mapped[dict] = mapped_column(JSONB, default=dict)  # rollup: sentiment, reply rate, churn delta
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignRecipient(Base):
    """Which contacts received which campaign message (needed for attribution)."""

    __tablename__ = "campaign_recipients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    email_address: Mapped[Optional[str]] = mapped_column(String)
    external_message_id: Mapped[Optional[str]] = mapped_column(String)  # ESP/per-send id
    rfc822_message_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class CampaignEvent(Base):
    """Engagement event from the ESP — open/click/bounce/unsubscribe/reply/convert."""

    __tablename__ = "campaign_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    recipient_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("campaign_recipients.id"))
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("contacts.id"))
    event_type: Mapped[str] = mapped_column(String, nullable=False)  # open|click|bounce|unsubscribe|reply|convert
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


# ──────────────────────────────────────────────────────────
# CONTINUOUS AI IMPROVEMENT
#
# Layer 1 — feedback capture (FeedbackEvent, TranscriptCorrection).
# Layer 2 — quality scoring (InsightQualityScore, EvaluationReferenceSet).
# Layer 3 — prompt versioning + experiments (PromptVariant, Experiment).
# Layer 4 — per-tenant personalisation (TenantPromptConfig).
# Layer 5 — ASR improvement (VocabularyCandidate, WerMetric).
# Layer 6/7 — orchestration metrics (CrossTenantAnalytic).
# ──────────────────────────────────────────────────────────


class FeedbackEvent(Base):
    """User feedback signal on any AI surface — both implicit + explicit."""

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
    # 'analysis' | 'email_classifier' | 'email_reply' | 'live_coaching'
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    signal_type: Mapped[str] = mapped_column(String, nullable=False)  # 'implicit' | 'explicit'
    insight_dimension: Mapped[Optional[str]] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TranscriptCorrection(Base):
    """Manual transcript edit — feeds vocabulary discovery and WER."""

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
    """Per-dimension quality score from the LLM judge or human reviewer."""

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
    """Versioned LLM prompt template, A/B-routable across the production surfaces.

    Status lifecycle: draft → shadow → canary → active → (rolled_back | retired).
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
    """Per-tenant prompt-time customisation — vocab, persona, few-shot, RAG, params."""

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
    """Candidate term discovered from corrections / low-confidence segments."""

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
    """Frozen, versioned snapshot of reference interactions for evaluation."""

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
    """A/B test, prompt-optimisation run, or other experiment record."""

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
    """Weekly word-error-rate aggregate per (tenant, engine, channel)."""

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
    """Cross-tenant aggregate metric — no ``tenant_id`` column by design.

    Tenants opted out via ``Tenant.features_enabled.data_use_for_improvement = false``
    are excluded from these aggregates upstream by the rollup job.
    """

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
