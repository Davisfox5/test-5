"""Zoom Phone UC adapter.

OAuth via the central oauth registry (entry ``zoom_phone``). Recording
lifecycle: ``phone.recording_completed`` event subscription on the
Zoom marketplace app → download URL embedded in payload (Bearer-auth'd).

Webhook authenticity: HMAC-SHA256 over ``v0:{timestamp}:{body}`` with
the Secret Token, delivered as ``v0={hex}`` in ``x-zm-signature``.
URL-validation handshake handled at the route layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import List, Mapping, Optional

import httpx

from backend.app.services.audio import AudioFormat
from backend.app.services.telephony.uc.base import (
    FetchedRecording,
    UCRecordingProvider,
    UCWebhookEvent,
    WebhookVerificationError,
    register,
)

logger = logging.getLogger(__name__)


class ZoomPhoneProvider(UCRecordingProvider):
    """Adapter for Zoom Phone's webhook + recording-download endpoints."""

    name = "zoom_phone"

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        signing_secret: str,
    ) -> UCWebhookEvent:
        signature = headers.get("x-zm-signature") or headers.get(
            "X-Zm-Signature"
        )
        timestamp = headers.get("x-zm-request-timestamp") or headers.get(
            "X-Zm-Request-Timestamp"
        )
        if not (signature and timestamp and signing_secret):
            raise WebhookVerificationError(
                "Missing Zoom signature, timestamp, or signing_secret"
            )

        msg = b"v0:" + timestamp.encode("utf-8") + b":" + body
        expected = (
            "v0="
            + hmac.new(
                signing_secret.encode("utf-8"), msg, hashlib.sha256
            ).hexdigest()
        )
        if not hmac.compare_digest(signature, expected):
            raise WebhookVerificationError("Zoom signature mismatch")

        try:
            int(timestamp)
        except (TypeError, ValueError) as exc:
            raise WebhookVerificationError(
                f"Zoom timestamp is not an integer: {timestamp!r}"
            ) from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError(
                f"Zoom webhook body is not JSON: {exc}"
            ) from exc

        event = payload.get("event")
        if event != "phone.recording_completed":
            raise WebhookVerificationError(
                f"Unsupported Zoom Phone event: {event!r}"
            )

        obj = (payload.get("payload") or {}).get("object") or {}
        call_id = obj.get("call_id") or obj.get("id")
        recording_files: List[dict] = obj.get("recording_files") or []
        recording = recording_files[0] if recording_files else {}
        recording_id = recording.get("id") or obj.get("recording_id")

        if not (call_id and recording_id):
            raise WebhookVerificationError(
                "Zoom Phone webhook missing call_id or recording id"
            )

        return UCWebhookEvent(
            provider=self.name,
            external_call_id=str(call_id),
            recording_id=str(recording_id),
            recording_url=recording.get("download_url"),
            duration_seconds=_safe_int(recording.get("duration")),
            started_at=obj.get("date_time")
            or recording.get("recording_start"),
            direction=_lower_or_none(obj.get("direction")),
            caller_phone=obj.get("caller_number")
            or obj.get("caller_did_number"),
            callee_phone=obj.get("callee_number")
            or obj.get("callee_did_number"),
            raw=payload,
        )

    async def fetch_recording(
        self,
        *,
        access_token: str,
        event: UCWebhookEvent,
    ) -> FetchedRecording:
        if not event.recording_url:
            raise WebhookVerificationError(
                "Zoom Phone event has no download URL — cannot fetch"
            )
        async with httpx.AsyncClient(
            timeout=120.0, follow_redirects=True
        ) as client:
            resp = await client.get(
                event.recording_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "audio/*",
                },
            )
            if resp.status_code >= 400:
                raise WebhookVerificationError(
                    f"Zoom recording fetch failed: {resp.status_code}"
                )
            content_type = (
                resp.headers.get("content-type") or "audio/mp4"
            ).split(";")[0].strip()
            return FetchedRecording(
                audio_bytes=resp.content,
                content_type=content_type,
                format_hint=_format_hint(content_type),
            )

    @staticmethod
    def url_validation_response(
        plain_token: str, secret_token: str
    ) -> dict:
        """Build the response body for Zoom's URL-validation handshake.

        Lives on the class because the route handler needs it before
        we even know which tenant or integration to attribute the
        delivery to. Pure function — no IO.
        """
        encrypted = hmac.new(
            secret_token.encode("utf-8"),
            plain_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {"plainToken": plain_token, "encryptedToken": encrypted}


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lower_or_none(value) -> Optional[str]:
    return str(value).lower() if value is not None else None


def _format_hint(content_type: str) -> Optional[AudioFormat]:
    ct = (content_type or "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return AudioFormat.MP3
    if "wav" in ct or "wave" in ct:
        return AudioFormat.WAV
    return None


register(ZoomPhoneProvider())
