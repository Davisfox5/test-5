"""SRTP master-key extraction for SIPREC SDES.

SIPREC INVITEs that use SDES (SDP Security Descriptions, RFC 4568)
carry the SRTP master key + salt as a base64 blob inside the
``a=crypto`` line on each audio media stream. The SRS sidecar
terminates SRTP and forwards plaintext audio frames to us, so this
module exists for two reasons:

1. Validation — we want to reject INVITEs with unsupported crypto
   suites at the bridge layer rather than having the SRS silently
   produce garbage audio.
2. Future-proofing — if we ever move SRTP termination into Python
   (we won't, for performance), the parsed key material is here.

DTLS-SRTP key exchange is **not** implemented in this module. Avaya
SBCE 8/10 and most modern Cisco CUBE deployments use DTLS-SRTP, not
SDES. For DTLS-SRTP, the SRS handles the DTLS handshake itself and
we never see the master key — we only need to know it's "in use".
That branch is documented as a v2 capability in the per-vendor docs.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

from backend.app.services.telephony.siprec.protocol import CryptoAttribute


# ── Supported suites (RFC 4568 §6.2 + RFC 6188 for AES-256) ─────────────


@dataclass(frozen=True)
class SuiteSpec:
    """Key + salt sizes for one named SRTP crypto suite."""

    suite: str
    master_key_bytes: int
    master_salt_bytes: int


# Only the suites we expect from Cisco CUBE / Avaya SBCE / Metaswitch
# are listed. Adding a suite here is a deliberate widening — vendors
# love to default to weak ciphers, and a quiet acceptance would mask
# a misconfiguration on the customer side.
_SUITES = {
    s.suite: s
    for s in (
        SuiteSpec("AES_CM_128_HMAC_SHA1_80", 16, 14),
        SuiteSpec("AES_CM_128_HMAC_SHA1_32", 16, 14),
        SuiteSpec("AES_256_CM_HMAC_SHA1_80", 32, 14),
        SuiteSpec("AES_256_CM_HMAC_SHA1_32", 32, 14),
    )
}


def supported_suites() -> tuple[str, ...]:
    """Return the SDES suite names the bridge will accept."""

    return tuple(_SUITES.keys())


def is_supported(suite: str) -> bool:
    return suite in _SUITES


# ── Key extraction ──────────────────────────────────────────────────────


@dataclass
class SrtpKeyMaterial:
    """Parsed SDES key material from a single ``a=crypto`` line.

    ``master_key`` and ``master_salt`` are raw bytes. ``lifetime`` and
    ``mki`` are advisory — most SBCs omit them, in which case the
    fields stay None and the receiver uses the protocol defaults.
    """

    suite: str
    master_key: bytes
    master_salt: bytes
    lifetime: Optional[str] = None
    mki: Optional[str] = None


def extract_key_material(crypto: CryptoAttribute) -> SrtpKeyMaterial:
    """Decode a ``CryptoAttribute`` into raw key + salt bytes.

    The key-params syntax (RFC 4568 §6.1) is:

    ::

        inline:<base64-key-and-salt>[|<lifetime>][|<mki>:<mki-length>]

    We split on ``|``, base64-decode the inline blob, and slice out
    the suite-defined key/salt sizes. Raises ``ValueError`` on any
    malformed or undersized key — better to fail at parse time than
    feed an SRS half a key.
    """

    spec = _SUITES.get(crypto.suite)
    if spec is None:
        raise ValueError(
            f"Unsupported SRTP crypto suite {crypto.suite!r}; "
            f"accepted: {', '.join(supported_suites())}"
        )

    parts = crypto.key_params.split("|")
    if not parts or not parts[0].lower().startswith("inline:"):
        raise ValueError(
            f"crypto key-params {crypto.key_params!r} is not an inline SDES key"
        )

    b64 = parts[0].split(":", 1)[1]
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError(f"crypto key is not valid base64: {exc}") from exc

    expected = spec.master_key_bytes + spec.master_salt_bytes
    if len(raw) < expected:
        raise ValueError(
            f"crypto key for {crypto.suite} is {len(raw)} bytes; "
            f"expected at least {expected}"
        )

    master_key = raw[: spec.master_key_bytes]
    master_salt = raw[spec.master_key_bytes : expected]

    lifetime: Optional[str] = None
    mki: Optional[str] = None
    if len(parts) >= 2:
        lifetime = parts[1] or None
    if len(parts) >= 3:
        mki = parts[2] or None

    return SrtpKeyMaterial(
        suite=crypto.suite,
        master_key=master_key,
        master_salt=master_salt,
        lifetime=lifetime,
        mki=mki,
    )


def select_crypto(
    crypto_attrs: list[CryptoAttribute],
) -> Optional[CryptoAttribute]:
    """Pick the strongest supported crypto attribute from an SDP offer.

    Preference order: AES_256 over AES_128, then SHA1_80 over
    SHA1_32 (longer auth tag). Returns ``None`` when no offered
    suite is supported — caller should reject the INVITE rather than
    fall back to plaintext RTP.
    """

    if not crypto_attrs:
        return None

    def rank(a: CryptoAttribute) -> tuple[int, int, int]:
        # Higher tuple = stronger.
        if a.suite not in _SUITES:
            return (-1, -1, -1)
        key_strength = 1 if a.suite.startswith("AES_256") else 0
        tag_strength = 1 if a.suite.endswith("_80") else 0
        # Prefer lower tag (earlier in offer) only when everything
        # else is equal, so the customer's preference order wins.
        return (key_strength, tag_strength, -a.tag)

    best = max(crypto_attrs, key=rank)
    return best if best.suite in _SUITES else None
