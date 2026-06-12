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
    # Public origin of the SPA — used to build OAuth-callback redirects
    # back into the app after a successful provider connect. Falls back
    # to the first allowed origin when unset (so staging "just works").
    SPA_URL: str = ""

    # ── Database (Neon PostgreSQL) ────────────────────────
    DATABASE_URL: str
    # Escape hatch: set True only if the runtime image lacks a CA bundle
    # and you accept encrypted-but-unauthenticated DB connections (the
    # pre-2026-06 behaviour). Default verifies the server certificate —
    # Neon presents a publicly-trusted cert and the Docker image ships
    # ca-certificates, so verification should always succeed in our
    # deployments.
    DATABASE_SSL_NO_VERIFY: bool = False

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
    # Canonical model ID per tier. Centralized so a model upgrade is a
    # config change instead of a 20-file sweep; services resolve these
    # via ``backend.app.services.llm_client.model_for_tier``.
    CLAUDE_HAIKU_MODEL: str = "claude-haiku-4-5-20251001"
    CLAUDE_SONNET_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_OPUS_MODEL: str = "claude-opus-4-7"
    # Route non-latency-sensitive LLM work (LLM-judge evaluations, the
    # weekly tenant-brief refiner) through the Anthropic Message Batches
    # API — identical results at a 50% token discount. Set False to fall
    # back to the old one-synchronous-call-per-item behaviour.
    LLM_BATCH_OFFLINE_JOBS: bool = True

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
    # QDRANT_URL is intentionally an empty default rather than
    # http://localhost:6333. The readiness probe at /api/v1/ready
    # treats an empty URL as "not configured" and skips the connect
    # attempt; the old localhost default caused the probe to fail on
    # every pgvector-only deployment (no qdrant in the container).
    # Set this to a real URL only when flipping VECTOR_BACKEND to "qdrant".
    QDRANT_URL: str = ""
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

    # ── OAuth — Google Workspace ─────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ── OAuth — Microsoft ────────────────────────────────
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""

    # ── OAuth — Slack (manager-alert delivery) ───────────
    SLACK_CLIENT_ID: str = ""
    SLACK_CLIENT_SECRET: str = ""
    SLACK_SIGNING_SECRET: str = ""

    # ── CRM ──────────────────────────────────────────────
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    PIPEDRIVE_CLIENT_ID: str = ""
    PIPEDRIVE_CLIENT_SECRET: str = ""
    # Stub providers — config slots only; the OAuth flow is marked
    # ``certified=False`` until adapters land.
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    MICROSOFT_DYNAMICS_CLIENT_ID: str = ""
    MICROSOFT_DYNAMICS_CLIENT_SECRET: str = ""

    # ── Contact Enrichment ───────────────────────────────
    PDL_API_KEY: str = ""
    APOLLO_API_KEY: str = ""

    # ── Stripe (billing) ─────────────────────────────────
    STRIPE_API_KEY: str = ""
    # ``whsec_...`` signing secret from the Stripe webhook endpoint.
    STRIPE_WEBHOOK_SECRET: str = ""
    # Optional second signing secret accepted in parallel with
    # ``STRIPE_WEBHOOK_SECRET`` while a rotation is in flight. See
    # ``services/stripe_billing.verify_stripe_signature_with_rotation``
    # for the rotation procedure.
    STRIPE_WEBHOOK_SECRET_NEXT: str = ""
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
    # JSON blob with every per-tier add-on price ID (extra scorecards,
    # additional seats, live coaching, onboarding fees). Drives the
    # ``/admin/stripe/checkout`` endpoint and the scorecard entitlement
    # check. Empty / missing / malformed → checkout disabled (503) and
    # paid-extra scorecards count as zero. Shape:
    #   {
    #     "starter": {"base": {"monthly":"price_..","annual":".."},
    #                 "addl_seat": {...}, "extra_scorecard": {...},
    #                 "onboarding": {"direct":"..","partner":".."}},
    #     "growth":  {... same shape ...},
    #     "enterprise": {... same shape ...},
    #     "starter_addons": {"live_coaching": {"monthly":"..","annual":".."}}
    #   }
    STRIPE_PRICE_CATALOG: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "us-east-1"

    # ── Encryption key for OAuth tokens at rest ──────────
    TOKEN_ENCRYPTION_KEY: str = ""  # 32-byte Fernet key

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
    # Optional, stronger Pub/Sub authentication: when set, the push
    # endpoint additionally requires a Google-signed OIDC token in the
    # Authorization header with this exact audience (configure the
    # push subscription with "Enable authentication" and set the
    # audience to the push URL). Unlike the ?token= secret, the OIDC
    # token never appears in access logs and can't be replayed after
    # expiry. The expected service-account email may be pinned too.
    GMAIL_PUSH_OIDC_AUDIENCE: str = ""
    GMAIL_PUSH_OIDC_SERVICE_ACCOUNT: str = ""
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
