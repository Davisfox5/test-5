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
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
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


class _ProfileBase:
    """Shared columns for all four profile tables.

    Each profile version is append-only; latest version per entity is
    the working truth.  ``source_event`` links back to the delta or
    reflection that produced this version for audit.
    """

    version: Mapped[int]
    profile: Mapped[dict]
    top_factors: Mapped[list]
    source_event: Mapped[dict]
    confidence: Mapped[Optional[float]]
    created_at: Mapped[datetime]


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

    ``tenant_id`` NULL denotes the global default; per-tenant overrides
    appear with that tenant's id.  Only one ``is_active`` row is
    permitted per ``(tenant_id, scorer_name)``.
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

    Each row represents one human correction of a scorer's output.  The
    events grow the golden evaluation set organically and feed back into
    the weekly recalibration pass.
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
