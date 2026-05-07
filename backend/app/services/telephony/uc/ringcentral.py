"""RingCentral UC adapter.

OAuth via the central oauth registry (entry ``ringcentral``).

Recording lifecycle: subscription → telephony-session webhook →
Call Log API recording fetch with Bearer auth.

Webhook authenticity is per-subscription verification token in the
``Verification-Token`` header, plus a Validation-Token handshake on
first delivery (handled at the route layer).
"""

from __future__ import annotations

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

_DEFAULT_API_BASE = "https://platform.ringcentral.com"


def _api_base(provider_config: Optional[dict]) -> str:
    raw = (provider_config or {}).get("api_base") or _DEFAULT_API_BASE
    return str(raw).rstrip("/")


class RingCentralProvider(UCRecordingProvider):
    """Adapter for RingCentral's webhook + Call Log Recording APIs."""

    name = "ringcentral"

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        signing_secret: str,
    ) -> UCWebhookEvent:
        provided = headers.get("verification-token") or headers.get(
            "Verification-Token"
        )
        if not provided or not signing_secret:
            raise WebhookVerificationError(
                "Missing verification-token header or signing_secret"
            )
        if not hmac.compare_digest(str(provided), str(signing_secret)):
            raise WebhookVerificationError(
                "RingCentral verification-token mismatch"
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError(
                f"RingCentral webhook body is not JSON: {exc}"
            ) from exc

        # Telephony-session events nest the actionable data under
        # ``body``; we accept either the top-level shape or the nested
        # one (RC's payload format has shifted across API versions).
        inner = payload.get("body") or payload
        parties = inner.get("parties") or []
        recording = None
        external_call_id = inner.get("telephonySessionId") or inner.get(
            "sessionId"
        )
        for party in parties:
            recs = party.get("recordings") or []
            if recs:
                recording = recs[0]
                if not external_call_id:
                    external_call_id = party.get("sessionId")
                break

        if recording is None:
            recording = inner.get("recording") or {}
        recording_id = recording.get("id") or inner.get("id")

        if not (external_call_id and recording_id):
            raise WebhookVerificationError(
                "RingCentral webhook payload missing call id or recording id"
            )

        return UCWebhookEvent(
            provider=self.name,
            external_call_id=str(external_call_id),
            recording_id=str(recording_id),
            recording_url=recording.get("contentUri") or recording.get("uri"),
            duration_seconds=_safe_int(recording.get("duration")),
            started_at=inner.get("startTime") or inner.get("creationTime"),
            direction=_lower_or_none(inner.get("direction")),
            caller_phone=_phone(inner.get("from")),
            callee_phone=_phone(inner.get("to")),
            raw=payload,
        )

    async def fetch_recording(
        self,
        *,
        access_token: str,
        event: UCWebhookEvent,
    ) -> FetchedRecording:
        api_base = _api_base(event.raw.get("__provider_config"))
        url = event.recording_url or (
            f"{api_base}/restapi/v1.0/account/~/recording/"
            f"{event.recording_id}/content"
        )

        async with httpx.AsyncClient(
            timeout=120.0, follow_redirects=True
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "audio/*",
                },
            )
            if resp.status_code >= 400:
                raise WebhookVerificationError(
                    f"RingCentral recording fetch failed: {resp.status_code}"
                )
            content_type = (
                resp.headers.get("content-type") or "audio/mpeg"
            ).split(";")[0].strip()
            return FetchedRecording(
                audio_bytes=resp.content,
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


def _lower_or_none(value) -> Optional[str]:
    return str(value).lower() if value is not None else None


def _phone(party) -> Optional[str]:
    if not isinstance(party, dict):
        return None
    return (
        party.get("phoneNumber")
        or party.get("extensionNumber")
        or party.get("name")
    )


def _format_hint(content_type: str) -> Optional[AudioFormat]:
    ct = (content_type or "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return AudioFormat.MP3
    if "wav" in ct or "wave" in ct:
        return AudioFormat.WAV
    return None


register(RingCentralProvider())
