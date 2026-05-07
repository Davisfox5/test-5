"""HMAC-SHA256 signature verification tests for AudioHook.

Covers:

* Round-trip: sign a known request, verify it, expect success.
* Tampering: any modification to a signed header / path / body
  must trigger ``SignatureVerificationError``.
* Missing signature input rejects.
* Algorithm pin (``hmac-sha256`` only).
* ``created`` skew window enforcement.
* Required-component enforcement (omitting ``audiohook-session-id``
  must reject even if the HMAC is otherwise valid).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest

from backend.app.services.telephony.audiohook.auth import (
    REQUIRED_SIGNED_COMPONENTS,
    SignatureVerificationError,
    build_signature_base,
    parse_signature_header,
    parse_signature_input,
    verify_audiohook_signature,
)


CLIENT_SECRET = "supersecret-do-not-leak"
ORG_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "00000000-0000-0000-0000-000000000abc"
CORRELATION_ID = "44444444-4444-4444-4444-444444444444"
API_KEY = "linda-test-key"


def _sign_headers(
    *,
    method: str = "GET",
    path: str = "/api/v1/audiohook/tenant-1",
    authority: str = "linda.example.com",
    created: int | None = None,
    secret: str = CLIENT_SECRET,
    components: tuple[str, ...] = REQUIRED_SIGNED_COMPONENTS,
    extra_headers: dict[str, str] | None = None,
    label: str = "sig1",
) -> dict[str, str]:
    """Mint a valid AudioHook-style signed header set for tests.

    Returns the full header dict the caller would receive on the
    upgrade request. Mirrors the production verification path step
    by step so a "valid" path here exercises the same signature
    base construction the verifier uses.
    """

    if created is None:
        created = int(time.time())
    headers = {
        "Audiohook-Organization-Id": ORG_ID,
        "Audiohook-Session-Id": SESSION_ID,
        "Audiohook-Correlation-Id": CORRELATION_ID,
        "X-API-KEY": API_KEY,
        "Host": authority,
    }
    if extra_headers:
        headers.update(extra_headers)

    components_block = " ".join(f'"{c}"' for c in components)
    sig_input_value = (
        f'({components_block});keyid="audiohook-key-1";alg="hmac-sha256";'
        f'created={created};nonce="abc123"'
    )
    sig_input_header = f"{label}={sig_input_value}"
    base = build_signature_base(
        components=components,
        method=method,
        target_path=path,
        authority=authority,
        headers=headers,
        signature_input_value=sig_input_value,
    )
    mac = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).digest()
    sig_header = f"{label}=:{base64.b64encode(mac).decode('ascii')}:"
    headers["Signature-Input"] = sig_input_header
    headers["Signature"] = sig_header
    return headers


# ── Round trip ─────────────────────────────────────────────────────────


def test_verify_passes_for_valid_signature():
    headers = _sign_headers()
    result = verify_audiohook_signature(
        method="GET",
        target_path="/api/v1/audiohook/tenant-1",
        authority="linda.example.com",
        headers=headers,
        client_secret=CLIENT_SECRET,
    )
    assert result.keyid == "audiohook-key-1"
    assert result.alg == "hmac-sha256"


def test_verify_passes_with_lowercase_header_keys():
    headers = _sign_headers()
    # FastAPI lowercases all header keys via ``websocket.headers.items()``.
    lower = {k.lower(): v for k, v in headers.items()}
    verify_audiohook_signature(
        method="GET",
        target_path="/api/v1/audiohook/tenant-1",
        authority="linda.example.com",
        headers=lower,
        client_secret=CLIENT_SECRET,
    )


# ── Tampering ──────────────────────────────────────────────────────────


def test_verify_rejects_tampered_session_id():
    headers = _sign_headers()
    headers["Audiohook-Session-Id"] = "tampered"
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-1",
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
        )


def test_verify_rejects_tampered_path():
    headers = _sign_headers(path="/api/v1/audiohook/tenant-1")
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-99",  # Different from signed
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
        )


def test_verify_rejects_wrong_secret():
    headers = _sign_headers(secret="wrong-key")
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-1",
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
        )


# ── Missing data ───────────────────────────────────────────────────────


def test_verify_rejects_unsigned_request():
    # Headers carry the AudioHook fields but NO Signature/Signature-Input.
    headers = {
        "Audiohook-Organization-Id": ORG_ID,
        "Audiohook-Session-Id": SESSION_ID,
        "Audiohook-Correlation-Id": CORRELATION_ID,
        "X-API-KEY": API_KEY,
        "Host": "linda.example.com",
    }
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-1",
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
        )


def test_verify_rejects_when_required_component_omitted():
    # Drop the audiohook-session-id from the SIGNED components list.
    components = tuple(
        c for c in REQUIRED_SIGNED_COMPONENTS if c != "audiohook-session-id"
    )
    headers = _sign_headers(components=components)
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-1",
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
        )


# ── Algorithm pin ──────────────────────────────────────────────────────


def test_verify_rejects_unsupported_algorithm():
    headers = _sign_headers()
    # Force the alg= parameter to something the verifier doesn't accept.
    headers["Signature-Input"] = headers["Signature-Input"].replace(
        'alg="hmac-sha256"', 'alg="rsa-pss-sha512"'
    )
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-1",
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
        )


# ── Created skew window ────────────────────────────────────────────────


def test_verify_rejects_expired_created_outside_window():
    headers = _sign_headers(created=int(time.time()) - 10_000)  # ~3 hours old
    with pytest.raises(SignatureVerificationError):
        verify_audiohook_signature(
            method="GET",
            target_path="/api/v1/audiohook/tenant-1",
            authority="linda.example.com",
            headers=headers,
            client_secret=CLIENT_SECRET,
            skew_seconds=600,
        )


def test_verify_passes_when_created_inside_window():
    headers = _sign_headers(created=int(time.time()) - 30)
    verify_audiohook_signature(
        method="GET",
        target_path="/api/v1/audiohook/tenant-1",
        authority="linda.example.com",
        headers=headers,
        client_secret=CLIENT_SECRET,
        skew_seconds=600,
    )


# ── Header parsing helpers ────────────────────────────────────────────


def test_parse_signature_input_extracts_components_and_params():
    raw = (
        'sig1=("@request-target" "@authority" "x-api-key");'
        'keyid="kid";alg="hmac-sha256";created=1700000000;nonce="abc"'
    )
    parsed = parse_signature_input(raw)
    assert "sig1" in parsed
    assert parsed["sig1"].components == (
        "@request-target",
        "@authority",
        "x-api-key",
    )
    assert parsed["sig1"].keyid == "kid"
    assert parsed["sig1"].alg == "hmac-sha256"
    assert parsed["sig1"].created == 1700000000


def test_parse_signature_header_decodes_byte_sequence():
    raw_bytes = b"\x01\x02\x03"
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    parsed = parse_signature_header(f"sig1=:{encoded}:")
    assert parsed["sig1"] == raw_bytes
