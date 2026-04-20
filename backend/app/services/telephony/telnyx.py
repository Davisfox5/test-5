"""Telnyx Call Control adapter.

Telnyx doesn't use TwiML. Instead it uses **Call Control**: every phase
of a call fires a webhook event, and the server answers by issuing a
REST command.

Typical inbound flow:

1. Telnyx webhook hits ``/telephony/telnyx/voice`` with
   ``event_type: "call.initiated"`` and a ``call_control_id``.
2. We respond with 200, then issue a REST ``POST /calls/{id}/actions/answer``
   + ``POST /calls/{id}/actions/streaming_start`` (with our WSS URL).
3. Telnyx opens our WS and streams base64 μ-law audio frames.
4. When the call ends we get ``call.hangup`` — our hangup handler
   dispatches batch analysis on the LiveSession, same as Twilio.

Webhook security: Telnyx signs payloads with **Ed25519**. The public key
is published in the portal and pinned per tenant (``public_key`` in the
Integration's ``provider_config``). We verify
``Telnyx-Signature-Ed25519`` and ``Telnyx-Timestamp`` on every webhook.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_REST_BASE = "https://api.telnyx.com/v2"
# Telnyx webhooks get rejected when their timestamp drifts past this
# many seconds from our clock. Five minutes matches Telnyx's own default.
_MAX_WEBHOOK_AGE_SECONDS = 300


def verify_telnyx_signature(
    *,
    public_key_base64: str,
    signature_header: str,
    timestamp_header: str,
    raw_body: bytes,
    now: Optional[float] = None,
) -> bool:
    """Verify Telnyx's Ed25519 webhook signature.

    Telnyx signs ``{timestamp}|{raw_body}`` with Ed25519. Returns False
    on any failure — caller should 403.
    """
    if not (public_key_base64 and signature_header and timestamp_header):
        return False
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else time.time()
    if abs(current - timestamp) > _MAX_WEBHOOK_AGE_SECONDS:
        return False

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover — cryptography is in reqs
        logger.warning("cryptography not installed; cannot verify Telnyx sig")
        return False

    try:
        key_bytes = base64.b64decode(public_key_base64)
        signature = base64.b64decode(signature_header)
    except (ValueError, TypeError):
        return False

    try:
        pubkey = Ed25519PublicKey.from_public_bytes(key_bytes)
    except Exception:
        return False

    signed_payload = f"{timestamp_header}|".encode("utf-8") + raw_body
    try:
        pubkey.verify(signature, signed_payload)
        return True
    except Exception:
        return False


def call_control_answer_url(call_control_id: str) -> str:
    return f"{_REST_BASE}/calls/{call_control_id}/actions/answer"


def call_control_streaming_start_url(call_control_id: str) -> str:
    return f"{_REST_BASE}/calls/{call_control_id}/actions/streaming_start"


def call_control_hangup_url(call_control_id: str) -> str:
    return f"{_REST_BASE}/calls/{call_control_id}/actions/hangup"


def call_control_dial_url() -> str:
    """Outbound dial endpoint."""
    return f"{_REST_BASE}/calls"


def call_control_hold_url(call_control_id: str) -> str:
    return f"{_REST_BASE}/calls/{call_control_id}/actions/hold"


def call_control_unhold_url(call_control_id: str) -> str:
    return f"{_REST_BASE}/calls/{call_control_id}/actions/unhold"


def call_control_transfer_url(call_control_id: str) -> str:
    return f"{_REST_BASE}/calls/{call_control_id}/actions/transfer"


def streaming_start_payload(
    *,
    stream_url: str,
    codec: str = "PCMU",
) -> dict:
    """Body for the streaming_start command.

    PCMU = G.711 μ-law. Telnyx defaults to base64-encoded frames over a
    bidirectional WebSocket, same shape Deepgram's live mulaw path wants.
    """
    return {
        "stream_url": stream_url,
        "stream_track": "both_tracks",
        "codec": codec,
    }
