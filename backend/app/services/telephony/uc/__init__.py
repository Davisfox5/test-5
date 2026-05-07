"""UC vendor recording integrations — Stream 2."""

from __future__ import annotations

from backend.app.services.telephony.uc.base import (
    FetchedRecording,
    UCRecordingProvider,
    UCWebhookEvent,
    WebhookVerificationError,
    get_provider,
)

__all__ = [
    "FetchedRecording",
    "UCRecordingProvider",
    "UCWebhookEvent",
    "WebhookVerificationError",
    "get_provider",
]
