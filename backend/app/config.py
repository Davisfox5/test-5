"""Application configuration — loads all secrets from .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Core ──────────────────────────────────────────────
    APP_NAME: str = "CallSight AI"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    # Default is empty so production deploys fail loud if CORS isn't configured.
    # Set to a comma-separated list of origins (e.g., "https://app.callsight.ai").
    # When DEBUG=True and this is empty, the middleware falls back to
    # "http://localhost:*" / "http://127.0.0.1:*" to keep local dev working.
    ALLOWED_ORIGINS: list[str] = []

    # ── Database (Neon PostgreSQL) ────────────────────────
    DATABASE_URL: str

    # ── Redis (ElastiCache) ──────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Auth (Clerk — transitional) ──────────────────────
    CLERK_SECRET_KEY: str = ""
    CLERK_PUBLISHABLE_KEY: str = ""

    # ── AI / LLM (Anthropic) ─────────────────────────────
    ANTHROPIC_API_KEY: str

    # ── Speech-to-Text (Deepgram) ────────────────────────
    DEEPGRAM_API_KEY: str = ""
    DEFAULT_TRANSCRIPTION_ENGINE: Literal["deepgram", "whisper"] = "deepgram"

    # ── Vector DB ────────────────────────────────────────
    # Which backend serves KB retrieval. "pgvector" is the default and requires
    # no extra infrastructure. Flip to "qdrant" after running a reindex once the
    # pgvector health signals start firing.
    VECTOR_BACKEND: Literal["pgvector", "qdrant"] = "pgvector"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""

    # ── Embeddings (Voyage AI) ───────────────────────────
    VOYAGE_API_KEY: str = ""
    VOYAGE_EMBED_MODEL: str = "voyage-3"
    # Must match the model's output dim (voyage-3 = 1024, voyage-3-large = 2048)
    VOYAGE_EMBED_DIM: int = 1024
    KB_CHUNK_TOKENS: int = 500
    KB_CHUNK_OVERLAP_TOKENS: int = 80

    # ── Vector health monitoring ─────────────────────────
    # Thresholds for the dev-only /admin/vector-health signal.
    VECTOR_HEALTH_P95_MS: int = 150
    VECTOR_HEALTH_SIZE_MILESTONES: list[int] = [1_000_000, 3_000_000, 5_000_000]
    VECTOR_HEALTH_ALERT_DAYS: int = 7
    # Optional: when set, auto-create a GitHub issue on sustained threshold breach.
    GITHUB_ALERT_REPO: str = ""  # e.g., "davisfox5/test-5"
    GITHUB_ALERT_TOKEN: str = ""

    # ── Full-Text Search (Elasticsearch) ─────────────────
    ELASTICSEARCH_URL: str = "http://localhost:9200"

    # ── Telephony ────────────────────────────────────────
    TELNYX_API_KEY: str = ""
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    SIGNALWIRE_PROJECT_ID: str = ""
    SIGNALWIRE_TOKEN: str = ""

    # ── Meeting Bots (Recall.ai) ─────────────────────────
    RECALL_AI_API_KEY: str = ""

    # ── WhatsApp (Meta Cloud API) ────────────────────────
    META_WHATSAPP_TOKEN: str = ""
    META_VERIFY_TOKEN: str = ""

    # ── OAuth — Google Workspace ─────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ── OAuth — Microsoft ────────────────────────────────
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""

    # ── CRM ──────────────────────────────────────────────
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    PIPEDRIVE_CLIENT_ID: str = ""
    PIPEDRIVE_CLIENT_SECRET: str = ""

    # ── Contact Enrichment ───────────────────────────────
    PDL_API_KEY: str = ""
    APOLLO_API_KEY: str = ""

    # ── AWS (S3 audio storage) ───────────────────────────
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "us-east-1"

    # ── Encryption key for OAuth tokens at rest ──────────
    TOKEN_ENCRYPTION_KEY: str = ""  # 32-byte Fernet key

    # ── Embeddings (optional — enables Qdrant RAG) ────────
    VOYAGE_API_KEY: str = ""

    # ── Email push notifications ──────────────────────────
    # Base URL the OAuth providers can reach to POST inbound
    # notifications.  Needs to be an https URL in production.
    PUBLIC_WEBHOOK_BASE_URL: str = ""
    # Gmail Pub/Sub topic the backend subscribes each mailbox to.
    # Format: "projects/{project}/topics/{topic}".  Must have the
    # Gmail service account granted Publisher on it.
    GMAIL_PUBSUB_TOPIC: str = ""
    # Shared secret between the backend and Google Pub/Sub push
    # subscription (passed as ?token=... on the push URL so we can
    # drop any untrusted caller fast).
    GMAIL_PUSH_TOKEN: str = ""
    # Shared "clientState" string Microsoft Graph echoes back to prove
    # the subscription owner is us.
    GRAPH_CLIENT_STATE: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
