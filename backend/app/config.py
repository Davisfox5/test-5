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
    APP_NAME: str = "LINDA"
    DEBUG: bool = False
    # Environment name used to tag Sentry events + metric labels so
    # staging noise doesn't bleed into prod dashboards.
    ENVIRONMENT: str = "local"
    # Deploy SHA / version. Populated by CI.
    RELEASE_VERSION: str = ""
    # Sentry DSN — empty disables error monitoring (local dev default).
    SENTRY_DSN: str = ""
    API_V1_PREFIX: str = "/api/v1"
    # Empty by default. Browsers reject ``Access-Control-Allow-Origin: *``
    # paired with ``Access-Control-Allow-Credentials: true``, so a wildcard
    # default would silently break every credentialed request in any
    # deployment that forgot to set the env var. Operators MUST set
    # explicit origins (e.g. https://linda-staging-app.fly.dev) via
    # ``ALLOWED_ORIGINS=["https://..."]`` in fly secrets / .env.
    ALLOWED_ORIGINS: list[str] = []

    # ── Database (Neon PostgreSQL) ────────────────────────
    DATABASE_URL: str

    # ── Redis (ElastiCache) ──────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Auth (Clerk — transitional) ──────────────────────
    CLERK_SECRET_KEY: str = ""
    CLERK_PUBLISHABLE_KEY: str = ""

    # ── Session JWTs (native per-user login) ─────────────
    # HMAC secret for signing browser session tokens. Must be ≥32 chars in
    # production. In DEBUG we fall back to an ephemeral value.
    SESSION_JWT_SECRET: str = ""
    SESSION_JWT_TTL_HOURS: int = 12

    # ── AI / LLM (Anthropic) ─────────────────────────────
    ANTHROPIC_API_KEY: str

    # ── Speech-to-Text (Deepgram) ────────────────────────
    DEEPGRAM_API_KEY: str = ""
    DEFAULT_TRANSCRIPTION_ENGINE: Literal["deepgram", "whisper"] = "deepgram"

    # ── Speaker Diarization (pyannote) ───────────────────
    # Needed to download pyannote/speaker-diarization-3.1 from HuggingFace
    # on first use. Accept the model's gated licence once per account.
    HUGGINGFACE_TOKEN: str = ""
    PYANNOTE_DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"

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

    # ── Stripe (billing) ─────────────────────────────────
    STRIPE_API_KEY: str = ""
    # ``whsec_...`` signing secret from the Stripe webhook endpoint.
    STRIPE_WEBHOOK_SECRET: str = ""
    # Price IDs that map 1:1 to plans.PLANS keys. Leave blank for tiers
    # you don't sell yet. Legacy STRIPE_PRICE_{SOLO,TEAM,PRO} env vars
    # are still honored as aliases for SANDBOX/STARTER/GROWTH at lookup
    # time (see stripe_billing.price_id_to_tier).
    STRIPE_PRICE_SANDBOX: str = ""
    STRIPE_PRICE_STARTER: str = ""
    STRIPE_PRICE_GROWTH: str = ""
    STRIPE_PRICE_ENTERPRISE: str = ""
    STRIPE_PRICE_SOLO: str = ""  # legacy alias → sandbox
    STRIPE_PRICE_TEAM: str = ""  # legacy alias → starter
    STRIPE_PRICE_PRO: str = ""  # legacy alias → growth
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

    # When True, the ingest poller hits every provider regardless of
    # whether Pub/Sub / Graph push is configured globally. Intended for
    # local dev + integration tests where setting up push is onerous.
    EMAIL_POLL_FORCE_ALL: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
