"""AES-256 token encryption wrapper.

Wraps Fernet (AES-128-CBC + HMAC-SHA256; despite the name, this is the
symmetric scheme the ``cryptography`` project ships for application-level
secret storage and is industry-standard for OAuth token at-rest encryption).

Keys:

* ``TOKEN_ENCRYPTION_KEY`` env var holds a URL-safe base64-encoded 32-byte
  Fernet key. Generate one with::

      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

* **Dev fallback**: when ``DEBUG=True`` and no key is set, we mint a
  per-process ephemeral key so local development doesn't require setup.
  Production deployments **must** set the env var; we log a loud warning
  otherwise so it can't silently happen in a real environment.

* **Legacy tolerance**: ``decrypt`` returns the value unchanged if it
  can't parse it as Fernet ciphertext (and logs a warning). This lets
  deployments whose Integration rows were written before this module
  existed continue working — the next refresh cycle writes the encrypted
  form, at which point the tolerant branch stops firing.

Public API:

* ``encrypt_token(s)`` — ``str | None`` in, ``str | None`` out. None/''
  pass through unchanged.
* ``decrypt_token(s)`` — inverse. Graceful on legacy plaintext.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


_FERNET_KEY_BYTES = 32


class TokenCryptoNotConfigured(RuntimeError):
    """Raised in production when we try to encrypt without a key."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    settings = get_settings()
    key = (settings.TOKEN_ENCRYPTION_KEY or os.environ.get("TOKEN_ENCRYPTION_KEY", "")).strip()

    if not key:
        if settings.DEBUG:
            # Ephemeral dev key. Logged loudly so it can't get confused with
            # a real deployment missing its key.
            generated = Fernet.generate_key().decode("ascii")
            logger.warning(
                "TOKEN_ENCRYPTION_KEY not set; generated an ephemeral key for DEBUG mode. "
                "Tokens encrypted this process will not be decryptable after restart."
            )
            return Fernet(generated.encode("ascii"))
        raise TokenCryptoNotConfigured(
            "TOKEN_ENCRYPTION_KEY must be set in production. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )

    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise TokenCryptoNotConfigured(
            f"TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {exc}"
        ) from exc


def encrypt_token(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a token string. Passes ``None``/empty through unchanged."""
    if not plaintext:
        return plaintext
    # If it's already encrypted (round-trip), don't double-encrypt.
    if _looks_encrypted(plaintext):
        return plaintext
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: Optional[str]) -> Optional[str]:
    """Decrypt a Fernet ciphertext. Returns legacy plaintext unchanged so
    existing deployments survive the rollout — once the next refresh fires
    we rewrite with the encrypted form."""
    if not ciphertext:
        return ciphertext
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.warning(
            "decrypt_token: value is not Fernet ciphertext — assuming legacy "
            "plaintext storage. Re-authenticate to upgrade."
        )
        return ciphertext


def _looks_encrypted(value: str) -> bool:
    """Quick sniff: Fernet tokens are URL-safe base64 of a fixed prefix and
    start with ``gAAAAA``. This lets us accept already-encrypted values on
    encrypt() (idempotent) without round-tripping them."""
    return isinstance(value, str) and value.startswith("gAAAAA") and len(value) > 40


def reset_cache_for_tests() -> None:
    """Test hook — clear the Fernet cache after mutating settings."""
    _fernet.cache_clear()
