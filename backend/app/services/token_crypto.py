"""Symmetric encryption for OAuth tokens at rest + signed OAuth state.

All Integration.access_token / refresh_token values are Fernet-encrypted
before they touch the database.  The key comes from the
``TOKEN_ENCRYPTION_KEY`` setting (a 32-byte url-safe base64 value — the
format Fernet expects).

The ``state`` parameter on OAuth authorize redirects is a time-limited
signed payload that carries tenant_id + user_id + nonce, so the callback
doesn't need a Redis round trip to match it back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from backend.app.config import get_settings


# ── Fernet key derivation ────────────────────────────────


def _derive_fernet_key() -> bytes:
    """Return a Fernet-compatible key derived from TOKEN_ENCRYPTION_KEY.

    Fernet requires 32 url-safe-base64-encoded bytes.  If the configured
    value is already that, we use it directly; otherwise we hash it with
    SHA-256 and base64-encode so any length secret works.
    """
    raw = get_settings().TOKEN_ENCRYPTION_KEY or "dev-only-token-key-change-me"
    # Try interpreting raw as an already-formatted Fernet key.
    try:
        Fernet(raw.encode() if isinstance(raw, str) else raw)
        return raw.encode() if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


_fernet: Optional[Fernet] = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_derive_fernet_key())
    return _fernet


def encrypt_token(plaintext: Optional[str]) -> Optional[str]:
    if plaintext is None:
        return None
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: Optional[str]) -> Optional[str]:
    if ciphertext is None:
        return None
    try:
        return _cipher().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Token stored before encryption landed — return as-is and let the
        # caller re-persist on the next refresh.
        return ciphertext


# ── Signed OAuth state tokens ────────────────────────────

# HMAC key for state tokens — separate surface from token encryption so
# rotating one doesn't invalidate the other.
def _state_key() -> bytes:
    raw = get_settings().TOKEN_ENCRYPTION_KEY or "dev-only-token-key-change-me"
    return hashlib.sha256(("state::" + raw).encode("utf-8")).digest()


STATE_TTL_SECONDS = 600


def sign_state(payload: Dict[str, Any]) -> str:
    """Return a url-safe string combining payload + HMAC signature."""
    body = {
        **payload,
        "iat": int(time.time()),
        "nonce": secrets.token_urlsafe(8),
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(_state_key(), raw, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")
    )


def verify_state(token: str) -> Dict[str, Any]:
    """Validate signature + TTL; return the decoded payload.

    Raises ``ValueError`` on any tampering or expiry.
    """
    try:
        b64_body, b64_sig = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Malformed state token") from exc

    def _pad(s: str) -> bytes:
        return (s + "=" * (-len(s) % 4)).encode("ascii")

    raw = base64.urlsafe_b64decode(_pad(b64_body))
    sig = base64.urlsafe_b64decode(_pad(b64_sig))
    expected = hmac.new(_state_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("State signature mismatch")

    body = json.loads(raw)
    if int(time.time()) - int(body.get("iat", 0)) > STATE_TTL_SECONDS:
        raise ValueError("State token expired")
    return body
