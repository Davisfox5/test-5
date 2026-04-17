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
    ALLOWED_ORIGINS: list[str] = ["*"]

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

    # ── Vector DB (Qdrant) ───────────────────────────────
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
