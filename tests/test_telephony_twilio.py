"""Tests for the Twilio helper module — TwiML build, signature check,
media decode."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from backend.app.services.telephony.twilio import (
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
)


# ── TwiML builder ──────────────────────────────────────────────────────


def test_build_voice_twiml_has_stream_verb():
    twiml = build_voice_twiml(
        session_id="abc-123",
        stream_url="wss://hooks.example.com/ws/telephony/twilio/abc-123",
    )
    assert twiml.startswith("<?xml")
    assert "<Connect>" in twiml
    assert '<Stream url="wss://hooks.example.com/ws/telephony/twilio/abc-123">' in twiml
    assert '<Parameter name="session_id" value="abc-123"/>' in twiml


def test_build_voice_twiml_embeds_greeting_when_provided():
    twiml = build_voice_twiml(
        session_id="s",
        stream_url="wss://x",
        greeting="This call may be recorded.",
    )
    assert "<Say>This call may be recorded.</Say>" in twiml


def test_build_voice_twiml_escapes_user_data():
    """Session ids should never inject into the XML (they come from our
    DB so this is defense-in-depth)."""
    twiml = build_voice_twiml(
        session_id='abc"><Reject/><!--',
        stream_url="wss://x?a=1&b=2",
    )
    assert '<Reject/>' not in twiml
    assert "&amp;" in twiml  # the & in the URL is escaped


# ── Signature validation ───────────────────────────────────────────────


def _compute_signature(auth_token: str, url: str, params: dict) -> str:
    concat = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), concat.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_signature_valid_roundtrip():
    token = "test-auth-token"
    url = "https://app.example.com/api/v1/telephony/twilio/voice?tenant_id=abc"
    params = {"CallSid": "CA1", "From": "+15551234", "To": "+15559876"}
    sig = _compute_signature(token, url, params)
    assert validate_twilio_signature(
        auth_token=token,
        request_url=url,
        params=params,
        signature_header=sig,
    )


def test_signature_wrong_token_rejected():
    url = "https://app.example.com/voice"
    params = {"CallSid": "CA1"}
    sig = _compute_signature("one-token", url, params)
    assert not validate_twilio_signature(
        auth_token="other-token",
        request_url=url,
        params=params,
        signature_header=sig,
    )


def test_signature_wrong_params_rejected():
    token = "tok"
    url = "https://app.example.com/voice"
    params = {"CallSid": "CA1"}
    sig = _compute_signature(token, url, params)
    assert not validate_twilio_signature(
        auth_token=token,
        request_url=url,
        params={"CallSid": "CA2"},  # tampered
        signature_header=sig,
    )


def test_signature_missing_header_is_rejected():
    assert not validate_twilio_signature(
        auth_token="tok",
        request_url="https://x",
        params={},
        signature_header="",
    )


def test_signature_missing_token_is_rejected():
    """Defensive: a blank configured token shouldn't accept arbitrary
    signatures (we'd rather 403 than silently allow)."""
    assert not validate_twilio_signature(
        auth_token="",
        request_url="https://x",
        params={},
        signature_header="anything",
    )


# ── Media payload decode ──────────────────────────────────────────────


def test_decode_media_payload_base64():
    # μ-law bytes 0xFF 0x7F (silence + max positive) base64-encoded.
    raw = bytes([0xFF, 0x7F, 0x00, 0x80])
    encoded = base64.b64encode(raw).decode("ascii")
    assert decode_media_payload(encoded) == raw


def test_decode_media_payload_empty_string():
    assert decode_media_payload("") == b""


def test_decode_media_payload_none_safe():
    """Defensive: Twilio should always send a string, but the helper
    must not crash if the caller passes ``None`` through by mistake."""
    assert decode_media_payload(None) == b""  # type: ignore[arg-type]
