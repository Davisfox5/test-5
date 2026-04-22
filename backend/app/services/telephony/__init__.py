"""Telephony adapters — Twilio / SignalWire / Telnyx Media Streams.

Scope is narrow: we accept inbound audio streams for live transcription
and parse provider webhooks. Call control (hold, transfer, outbound
dial, recording) lives in the tenant's phone system, not LINDA.
"""

from backend.app.services.telephony.twilio import (
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
)

__all__ = [
    "build_voice_twiml",
    "decode_media_payload",
    "validate_twilio_signature",
]
