"""Telephony adapters — Twilio / SignalWire / Telnyx Media Streams.

Scope is narrow: we accept inbound audio streams for live transcription
and parse provider webhooks. Call control (hold, transfer, outbound
dial, recording) lives in the tenant's phone system, not LINDA.
"""

from typing import Literal

from backend.app.services.telephony.twilio import (
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
)

# ── Reserved Integration.provider namespace ─────────────────────────────
# Every value the codebase writes into ``Integration.provider`` for a
# telephony integration must come from this Literal. Adding a new
# value here is the *only* sanctioned way to introduce a new provider
# string — collisions across streams would otherwise be a runtime bug.
# Coordinate additions via the plan doc, not silent edits.
# See: /Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md
TelephonyProvider = Literal[
    # Existing CPaaS adapters (frozen for the multi-stream work):
    "twilio",
    "signalwire",
    "telnyx",
    # Stream 1 — SIPREC sources (one Session Recording Server, three
    # SBC config templates):
    "siprec_cisco_cube",
    "siprec_avaya_sbce",
    "siprec_metaswitch",
    # Stream 2 — UC vendor APIs (OAuth + webhook + recording fetch):
    "ringcentral",
    "webex_calling",
    "zoom_phone",
    # Stream 3 — Microsoft Teams compliance recording (scaffold only
    # this round; full media bot deferred to a follow-on stream):
    "teams_compliance",
    # Stream 4 — Genesys Cloud AudioHook (WebSocket streaming):
    "genesys_audiohook",
]


__all__ = [
    "TelephonyProvider",
    "build_voice_twiml",
    "decode_media_payload",
    "validate_twilio_signature",
]
