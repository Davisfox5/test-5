"""Symmetric encryption for OAuth tokens at rest + signed OAuth state.

Token encryption wraps Fernet (AES-128-CBC + HMAC-SHA256; despite the name,
this is the symmetric scheme the ``cryptography`` project ships for
application-level secret storage and is industry-standard for OAuth token
at-rest encryption).

Keys:

* ``TOKEN_ENCRYPTION_KEY`` env var holds a URL-safe base64-encoded 32-byte
  Fernet key. Generate one with::

      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

* **Dev fallback**: when ``DEBUG=True`` and no key is set, we mint a
  per-process ephemeral key so local development doesn't require setup.
  Production deployments **must** set the env var; we raise
  ``TokenCryptoNotConfigured`` otherwise so it can't silently happen in a
  real environment.

* **Legacy tolerance**: ``decrypt_token`` returns the value unchanged if it
  can't parse it as Fernet ciphertext (and logs a warning). This lets
  deployments whose ``Integration`` rows were written before this module
  existed continue working — the next refresh cycle (or the one-time
  re-encryption migration) writes the encrypted form, at which point the
  tolerant branch stops firing.

Signed OAuth state:

* ``sign_state`` / ``verify_state`` implement a short-lived HMAC-signed
  state token for the OAuth authorize -> callback round trip. The payload
  carries tenant_id + user_id + nonce + issued-at, so the callback can
  validate without a Redis round trip. TTL defaults to 10 min.

Public API:

* ``encrypt_token(s)`` -- ``str | None`` in, ``str | None`` out. None/''
  pass through unchanged.
* ``decrypt_token(s)`` -- inverse. Graceful on legacy plaintext.
* ``looks_encrypted(s)`` -- quick sniff so migrations can skip already-
  encrypted rows.
* ``sign_state(payload)`` -- returns a url-safe token.
* ``verify_state(token)`` -- validates signature + TTL and returns the
  payload.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from functools import lru_cache
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from backend.app.config import get_settings


logger = logging.getLogger(__name__)


class TokenCryptoNotConfigured(RuntimeError):
    """Raised when ``TOKEN_ENCRYPTION_KEY`` is missing in a non-debug context."""


# -- Fernet key acquisition -------------------------------


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    settings = get_settings()
    raw = (settings.TOKEN_ENCRYPTION_KEY or "").strip()

    if raw:
        try:
            return Fernet(raw.encode() if isinstance(raw, str) else raw)
        except (ValueError, TypeError) as exc:
            raise TokenCryptoNotConfigured(
                "TOKEN_ENCRYPTION_KEY is set but not a valid 32-byte url-safe base64 "
                "Fernet key. Generate one with: python -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\""
            ) from exc

    if not settings.DEBUG:
        raise TokenCryptoNotConfigured(
            "TOKEN_ENCRYPTION_KEY is required in non-debug environments. Set it to a "
            "32-byte url-safe base64 Fernet key."
        )

    logger.warning(
        "TOKEN_ENCRYPTION_KEY is unset; using an ephemeral per-process key because "
        "DEBUG=True. Existing encrypted tokens in the DB will be unreadable after "
        "this process restarts."
    )
    return Fernet(Fernet.generate_key())


def encrypt_token(plaintext: Optional[str]) -> Optional[str]:
    """Fernet-encrypt a string. ``None``/empty pass through unchanged."""
    if plaintext is None or plaintext == "":
        return plaintext
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: Optional[str]) -> Optional[str]:
    """Inverse of :func:`encrypt_token`. Legacy plaintext returns unchanged."""
    if ciphertext is None or ciphertext == "":
        return ciphertext
    try:
        return _cipher().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning(
            "decrypt_token: value does not parse as Fernet ciphertext; returning "
            "unchanged (legacy-plaintext path). Will be re-encrypted on next write."
        )
        return ciphertext


def looks_encrypted(value: Optional[str]) -> bool:
    """Quick sniff to decide whether a stored value is already Fernet ciphertext."""
    if not value:
        return False
    try:
        _cipher().decrypt(value.encode("utf-8"))
        return True
    except (InvalidToken, ValueError):
        return False


# Back-compat alias for older callers that imported the sniff under the
# "private" name.
_looks_encrypted = looks_encrypted


# -- Signed OAuth state tokens ----------------------------


def _state_key() -> bytes:
    """HMAC key for state tokens, derived separately from the token-encryption key."""
    settings = get_settings()
    raw = (settings.TOKEN_ENCRYPTION_KEY or "").strip()
    if not raw:
        if not settings.DEBUG:
            raise TokenCryptoNotConfigured(
                "TOKEN_ENCRYPTION_KEY is required for signed OAuth state."
            )
        raw = os.environ.setdefault("_CALLSIGHT_DEV_STATE_KEY", secrets.token_urlsafe(32))
    return hashlib.sha256(("state::" + raw).encode("utf-8")).digest()


STATE_TTL_SECONDS = 600


def sign_state(payload: Dict[str, Any]) -> str:
    """Return a url-safe string combining payload + HMAC-SHA256 signature."""
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


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_state(token: str) -> Dict[str, Any]:
    """Validate signature + TTL; return the decoded payload. Raises ValueError on failure."""
    try:
        body_b64, mac_b64 = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("state token is not in body.mac format") from exc

    try:
        raw = _b64url_decode(body_b64)
        mac = _b64url_decode(mac_b64)
    except Exception as exc:
        raise ValueError("state token base64 decode failed") from exc

    expected = hmac.new(_state_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("state token signature mismatch")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("state token payload is not valid JSON") from exc

    iat = payload.get("iat")
    if not isinstance(iat, int) or time.time() - iat > STATE_TTL_SECONDS:
        raise ValueError("state token expired")

    return payload
