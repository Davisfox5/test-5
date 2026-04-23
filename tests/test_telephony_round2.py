"""Tests for telephony primitives still in scope after the 2026-04
refactor: SignalWire URL builders and Telnyx Ed25519 signature
verification. Live-stream ingress keeps the Twilio / SignalWire /
Telnyx webhooks + WebSockets; call control (hold/transfer/recording)
is owned by the tenant's phone system, not LINDA.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.app.services import s3_audio
from backend.app.services.telephony.signalwire import (
    build_calls_url,
    build_update_call_url,
    signalwire_rest_base,
)
from backend.app.services.telephony.telnyx import (
    call_control_answer_url,
    call_control_dial_url,
    call_control_hangup_url,
    call_control_streaming_start_url,
    streaming_start_payload,
    verify_telnyx_signature,
)
from backend.app.services.telephony.twilio import build_voice_twiml


# ── Voice TwiML ──────────────────────────────────────────────────────


def test_voice_twiml_has_stream_parameter_with_session_id():
    twiml = build_voice_twiml(
        session_id="s1", stream_url="wss://x/ws"
    )
    assert "<Connect>" in twiml
    assert '<Stream url="wss://x/ws">' in twiml
    assert '<Parameter name="session_id" value="s1"/>' in twiml


def test_voice_twiml_includes_greeting_when_provided():
    twiml = build_voice_twiml(
        session_id="s1", stream_url="wss://x", greeting="Hello"
    )
    assert "<Say>Hello</Say>" in twiml


# ── S3 staging helpers ──────────────────────────────────────────────


def test_s3_key_layout_is_stable_per_recording():
    key = s3_audio._build_key("t-abc", "r-123", "audio/wav")
    assert key == "recordings/t-abc/r-123.wav"


def test_s3_key_picks_extension_from_content_type():
    assert s3_audio._content_type_extension("audio/wav") == "wav"
    assert s3_audio._content_type_extension("audio/mpeg") == "mp3"
    assert s3_audio._content_type_extension("application/octet-stream") == "bin"
    assert s3_audio._content_type_extension("") == "bin"


def test_s3_key_handles_content_type_with_charset():
    # Real responses sometimes send "audio/wav; charset=binary".
    assert s3_audio._content_type_extension("audio/wav; charset=binary") == "wav"


def test_upload_bytes_raises_when_bucket_missing(monkeypatch):
    """Defensive: uploading without AWS_S3_BUCKET configured must raise
    rather than silently drop the audio."""
    from backend.app.services import s3_audio as mod
    from types import SimpleNamespace

    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(
            AWS_S3_BUCKET="",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="",
            AWS_SECRET_ACCESS_KEY="",
        ),
    )
    with pytest.raises(mod.S3NotConfigured):
        mod.upload_bytes(
            tenant_id="t",
            recording_id="r",
            data=b"audio",
            content_type="audio/wav",
        )


# ── SignalWire URL helpers ───────────────────────────────────────────


def test_signalwire_rest_base_accepts_bare_domain():
    assert signalwire_rest_base("acme.signalwire.com") == (
        "https://acme.signalwire.com/api/laml/2010-04-01"
    )


def test_signalwire_rest_base_accepts_full_url():
    assert signalwire_rest_base("https://acme.signalwire.com/") == (
        "https://acme.signalwire.com/api/laml/2010-04-01"
    )


def test_signalwire_rest_base_requires_space_url():
    with pytest.raises(ValueError):
        signalwire_rest_base("")


def test_signalwire_calls_url_format():
    url = build_calls_url("acme.signalwire.com", "p1")
    assert url.endswith("/Accounts/p1/Calls.json")


def test_signalwire_update_call_url_format():
    url = build_update_call_url("acme.signalwire.com", "p1", "CA123")
    assert url.endswith("/Accounts/p1/Calls/CA123.json")


# ── Telnyx URLs + signature ──────────────────────────────────────────


def test_telnyx_control_urls_are_stable():
    assert call_control_answer_url("abc").endswith("/calls/abc/actions/answer")
    assert call_control_hangup_url("abc").endswith("/calls/abc/actions/hangup")
    assert call_control_streaming_start_url("abc").endswith(
        "/calls/abc/actions/streaming_start"
    )
    assert call_control_dial_url().endswith("/calls")


def test_streaming_start_payload_defaults_to_mulaw():
    p = streaming_start_payload(stream_url="wss://x")
    assert p["codec"] == "PCMU"
    assert p["stream_track"] == "both_tracks"
    assert p["stream_url"] == "wss://x"


def _ed25519_sign(private: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    sig = private.sign(f"{timestamp}|".encode() + body)
    return base64.b64encode(sig).decode()


def _public_key_b64(private: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    pub = private.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    return base64.b64encode(pub).decode()


def test_telnyx_signature_valid_roundtrip():
    private = Ed25519PrivateKey.generate()
    body = b'{"event_type":"call.initiated"}'
    ts = str(int(time.time()))
    sig = _ed25519_sign(private, ts, body)
    assert verify_telnyx_signature(
        public_key_base64=_public_key_b64(private),
        signature_header=sig,
        timestamp_header=ts,
        raw_body=body,
    )


def test_telnyx_signature_tampered_body_rejected():
    private = Ed25519PrivateKey.generate()
    body = b'{"event_type":"call.initiated"}'
    ts = str(int(time.time()))
    sig = _ed25519_sign(private, ts, body)
    assert not verify_telnyx_signature(
        public_key_base64=_public_key_b64(private),
        signature_header=sig,
        timestamp_header=ts,
        raw_body=b'{"event_type":"call.tampered"}',
    )


def test_telnyx_signature_wrong_key_rejected():
    signer = Ed25519PrivateKey.generate()
    body = b"payload"
    ts = str(int(time.time()))
    sig = _ed25519_sign(signer, ts, body)
    # Verify with a different key.
    other = Ed25519PrivateKey.generate()
    assert not verify_telnyx_signature(
        public_key_base64=_public_key_b64(other),
        signature_header=sig,
        timestamp_header=ts,
        raw_body=body,
    )


def test_telnyx_signature_old_timestamp_rejected():
    private = Ed25519PrivateKey.generate()
    body = b"payload"
    old_ts = str(int(time.time()) - 3600)  # one hour ago
    sig = _ed25519_sign(private, old_ts, body)
    assert not verify_telnyx_signature(
        public_key_base64=_public_key_b64(private),
        signature_header=sig,
        timestamp_header=old_ts,
        raw_body=body,
    )


def test_telnyx_signature_empty_headers_rejected():
    private = Ed25519PrivateKey.generate()
    assert not verify_telnyx_signature(
        public_key_base64=_public_key_b64(private),
        signature_header="",
        timestamp_header="",
        raw_body=b"x",
    )


def test_telnyx_signature_bad_base64_rejected():
    private = Ed25519PrivateKey.generate()
    assert not verify_telnyx_signature(
        public_key_base64=_public_key_b64(private),
        signature_header="not_base64!!!",
        timestamp_header=str(int(time.time())),
        raw_body=b"x",
    )
