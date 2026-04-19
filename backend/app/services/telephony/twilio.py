"""Twilio adapter helpers.

Two surfaces:

1. **Inbound voice webhook** (``POST /telephony/twilio/voice``) returns
   TwiML that bridges the caller's audio into our WebSocket via Twilio
   Media Streams. The TwiML ``<Connect><Stream url="wss://…"/></Connect>``
   verb opens a bidirectional audio socket keyed by ``session_id``.

2. **Media Streams WebSocket** (``/ws/telephony/twilio/{session_id}``)
   accepts Twilio's JSON framing — ``event: connected|start|media|stop``
   — and forwards the base64-encoded μ-law audio on to Deepgram.

This module only owns the pure helpers (TwiML builder, signature
validator, payload decoder). The actual WS handler lives in
``backend.app.api.telephony`` so it can coexist with our existing
``/ws/live/{session_id}`` handler.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Dict, Optional
from xml.sax.saxutils import escape as _xml_escape


def build_voice_twiml(
    *,
    session_id: str,
    stream_url: str,
    greeting: Optional[str] = None,
    record: bool = False,
    recording_status_callback_url: Optional[str] = None,
) -> str:
    """Return the TwiML Twilio should execute when a call comes in.

    ``<Connect><Stream>`` opens a bidirectional audio pipe to
    ``stream_url`` (wss://…). We tag each stream with a ``<Parameter>``
    carrying ``session_id`` so the downstream handler can correlate
    the socket to a LiveSession row before the first audio arrives.

    ``greeting`` is an optional ``<Say>`` pre-roll (useful for
    compliance disclosures).

    ``record=True`` adds a ``<Record>`` with ``recordingStatusCallback``
    so Twilio POSTs us when the audio is ready to pull. We use Twilio's
    ``record="record-from-answer-dual"`` mode on ``<Start>`` instead of
    ``<Record>`` to capture both sides without blocking the Stream. The
    status callback URL is XML-escaped.
    """
    safe_url = _xml_escape(stream_url, {'"': "&quot;"})
    safe_session = _xml_escape(session_id, {'"': "&quot;"})
    pre = ""
    if greeting:
        pre = f"  <Say>{_xml_escape(greeting)}</Say>\n"

    record_verb = ""
    if record and recording_status_callback_url:
        safe_cb = _xml_escape(recording_status_callback_url, {'"': "&quot;"})
        record_verb = (
            "  <Start>\n"
            f'    <Recording recordingStatusCallback="{safe_cb}" '
            'recordingStatusCallbackEvent="completed" '
            'recordingTrack="both"/>\n'
            "  </Start>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"{pre}"
        f"{record_verb}"
        "  <Connect>\n"
        f'    <Stream url="{safe_url}">\n'
        f'      <Parameter name="session_id" value="{safe_session}"/>\n'
        "    </Stream>\n"
        "  </Connect>\n"
        "</Response>"
    )


def build_hold_twiml(
    *,
    hold_music_url: str = "https://com.twilio.sounds.music.s3.amazonaws.com/ClockworkWaltz.mp3",
    loop_count: int = 0,
) -> str:
    """TwiML that plays hold music in a loop until the call is redirected
    back to its main flow. ``loop_count=0`` means loop forever."""
    safe_url = _xml_escape(hold_music_url, {'"': "&quot;"})
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Play loop="{int(loop_count)}">{safe_url}</Play>\n'
        "</Response>"
    )


def build_transfer_twiml(*, to_number: str, caller_id: Optional[str] = None) -> str:
    """TwiML that dials ``to_number`` — used when an agent hits the
    transfer button. Once the callee answers Twilio bridges the two legs.

    When ``caller_id`` is provided it's set on the <Dial> so the callee
    sees the original caller (assuming Twilio permits the override).
    """
    safe_to = _xml_escape(to_number, {'"': "&quot;"})
    caller_attr = ""
    if caller_id:
        caller_attr = f' callerId="{_xml_escape(caller_id, {chr(34): "&quot;"})}"'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Dial{caller_attr}>{safe_to}</Dial>\n'
        "</Response>"
    )


def validate_twilio_signature(
    *,
    auth_token: str,
    request_url: str,
    params: Dict[str, str],
    signature_header: str,
) -> bool:
    """Verify Twilio's ``X-Twilio-Signature`` header.

    Twilio's algorithm (from their webhook security docs):

    1. Take the full request URL (including query string).
    2. Sort the POST form params by key and concatenate
       ``key + value`` for each.
    3. Append the concatenation to the URL.
    4. HMAC-SHA1 with the auth token as the key.
    5. Base64-encode the digest.

    Compare against the header value in constant time.
    """
    if not auth_token or not signature_header:
        return False
    concat = request_url
    for key in sorted(params.keys()):
        concat += key + (params[key] if params[key] is not None else "")
    digest = hmac.new(
        auth_token.encode("utf-8"),
        concat.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature_header.strip())


def decode_media_payload(payload: str) -> bytes:
    """Twilio Media Streams sends μ-law audio as base64 in the JSON
    ``media.payload`` field. Decode to raw bytes. μ-law at 8 kHz is
    what Deepgram's ``encoding=mulaw&sample_rate=8000`` expects."""
    if not payload:
        return b""
    return base64.b64decode(payload)
