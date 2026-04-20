"""Tests for the Fernet token-encryption wrapper."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from backend.app.services import token_crypto
from backend.app.services.token_crypto import (
    TokenCryptoNotConfigured,
    _looks_encrypted,
    decrypt_token,
    encrypt_token,
    reset_cache_for_tests,
)


@pytest.fixture
def fernet_key():
    """Force a stable Fernet key for deterministic tests."""
    key = Fernet.generate_key().decode("ascii")
    with patch.object(
        token_crypto, "get_settings"
    ) as gs:
        gs.return_value.TOKEN_ENCRYPTION_KEY = key
        gs.return_value.DEBUG = False
        reset_cache_for_tests()
        yield key
    reset_cache_for_tests()


def test_roundtrip_preserves_plaintext(fernet_key):
    ciphertext = encrypt_token("super-secret-refresh")
    assert ciphertext != "super-secret-refresh"
    assert decrypt_token(ciphertext) == "super-secret-refresh"


def test_none_and_empty_passthrough(fernet_key):
    assert encrypt_token(None) is None
    assert encrypt_token("") == ""
    assert decrypt_token(None) is None
    assert decrypt_token("") == ""


def test_encrypt_is_idempotent_on_already_encrypted(fernet_key):
    ct = encrypt_token("token")
    again = encrypt_token(ct)
    # Same ciphertext returned — we don't double-encrypt.
    assert again == ct
    assert decrypt_token(again) == "token"


def test_decrypt_returns_legacy_plaintext_unchanged(fernet_key):
    # Values written before we wired encryption should not raise.
    assert decrypt_token("plain-token-abc") == "plain-token-abc"


def test_missing_key_in_production_raises():
    with patch.object(token_crypto, "get_settings") as gs:
        gs.return_value.TOKEN_ENCRYPTION_KEY = ""
        gs.return_value.DEBUG = False
        reset_cache_for_tests()
        try:
            with pytest.raises(TokenCryptoNotConfigured):
                encrypt_token("t")
        finally:
            reset_cache_for_tests()


def test_debug_mode_generates_ephemeral_key():
    with patch.object(token_crypto, "get_settings") as gs:
        gs.return_value.TOKEN_ENCRYPTION_KEY = ""
        gs.return_value.DEBUG = True
        reset_cache_for_tests()
        try:
            ct = encrypt_token("hello")
            assert decrypt_token(ct) == "hello"
        finally:
            reset_cache_for_tests()


def test_looks_encrypted_sniff():
    assert _looks_encrypted("gAAAAAB12345" + "a" * 50)
    assert not _looks_encrypted("plain")
    assert not _looks_encrypted("")
    assert not _looks_encrypted("gAAAAA")  # too short
