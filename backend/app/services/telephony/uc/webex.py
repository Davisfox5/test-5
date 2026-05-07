"""Webex Calling UC adapter.

OAuth via the central oauth registry (entry ``webex_calling``, PKCE
enabled). Recording lifecycle: webhook subscription on ``recordings``
resource → Converged Recordings API metadata → temporary direct
download link.

Webhook authenticity: HMAC-SHA1 over body with the per-tenant secret,
delivered as ``X-Spark-Signature``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Mapping, Optional

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

_DEFAULT_API_BASE = "https://webexapis.com"


def _api_base(provider_config: Optional[dict]) -> str:
    raw = (provider_config or {}).get("api_base") or _DEFAULT_API_BASE
    return str(raw).rstrip("/")


class WebexProvider(UCRecordingProvider):
    """Adapter for Webex Webhooks + Converged Recordings APIs."""

    name = "webex_calling"

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        signing_secret: str,
    ) -> UCWebhookEvent:
        provided = headers.get("x-spark-signature") or headers.get(
            "X-Spark-Signature"
        )
        if not provided or not signing_secret:
            raise WebhookVerificationError(
                "Missing X-Spark-Signature header or signing_secret"
            )
        expected = hmac.new(
            signing_secret.encode("utf-8"), body, hashlib.sha1
        ).hexdigest()
        if not hmac.compare_digest(str(provided).lower(), expected.lower()):
            raise WebhookVerificationError("Webex X-Spark-Signature mismatch")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError(
                f"Webex webhook body is not JSON: {exc}"
            ) from exc

        resource = payload.get("resource")
        event = payload.get("event")
        if resource != "recordings" or event not in ("created", "completed"):
            raise WebhookVerificationError(
                f"Unsupported Webex event: resource={resource!r} event={event!r}"
            )

        data = payload.get("data") or {}
        recording_id = data.get("id")
        if not recording_id:
            raise WebhookVerificationError(
                "Webex webhook missing data.id (recording id)"
            )

        return UCWebhookEvent(
            provider=self.name,
            external_call_id=str(recording_id),
            recording_id=str(recording_id),
            recording_url=None,  # resolved at fetch time
            duration_seconds=_safe_int(data.get("durationSeconds")),
            started_at=data.get("createTime") or data.get("created"),
            direction=None,
            caller_phone=None,
            callee_phone=None,
            raw=payload,
        )

    async def fetch_recording(
        self,
        *,
        access_token: str,
        event: UCWebhookEvent,
    ) -> FetchedRecording:
        provider_config = event.raw.get("__provider_config") or {}
        api_base = _api_base(provider_config)

        async with httpx.AsyncClient(
            timeout=120.0, follow_redirects=True
        ) as client:
            meta_resp = await client.get(
                f"{api_base}/v1/recordings/{event.recording_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            if meta_resp.status_code >= 400:
                raise WebhookVerificationError(
                    f"Webex recording metadata fetch failed: "
                    f"{meta_resp.status_code}"
                )
            meta = meta_resp.json()
            links = meta.get("temporaryDirectDownloadLinks") or {}
            audio_url = (
                links.get("audioDownloadLink")
                or meta.get("downloadUrl")
                or links.get("recordingDownloadLink")
            )
            if not audio_url:
                raise WebhookVerificationError(
                    "Webex recording has no audio download link"
                )

            # Temporary URL is signed; Authorization header is NOT
            # required (Webex rejects requests that include it).
            audio_resp = await client.get(audio_url)
            if audio_resp.status_code >= 400:
                raise WebhookVerificationError(
                    f"Webex audio download failed: {audio_resp.status_code}"
                )
            content_type = (
                audio_resp.headers.get("content-type")
                or meta.get("format")
                or "audio/mp4"
            ).split(";")[0].strip()
            return FetchedRecording(
                audio_bytes=audio_resp.content,
                content_type=content_type,
                format_hint=_format_hint(content_type),
            )


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_hint(content_type: str) -> Optional[AudioFormat]:
    ct = (content_type or "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return AudioFormat.MP3
    if "wav" in ct or "wave" in ct:
        return AudioFormat.WAV
    return None


register(WebexProvider())
