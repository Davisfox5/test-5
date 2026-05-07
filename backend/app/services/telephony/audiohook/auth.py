"""HMAC-SHA256 verification of the AudioHook upgrade request.

Genesys Cloud signs the WebSocket upgrade GET request with the
integration's client secret using the IETF "HTTP Message Signatures"
draft / RFC 9421 vocabulary. Each signed request carries:

* ``Signature-Input`` — names the components included in the signature
  base, plus ``keyid``, ``alg`` (= ``hmac-sha256``), ``created``,
  ``nonce``.
* ``Signature`` — base64-encoded HMAC over the signature base.
* ``X-API-KEY`` — the API key the integration was provisioned with;
  Genesys also signs this header so it can't be swapped out.
* ``Audiohook-Organization-Id`` / ``Audiohook-Session-Id`` /
  ``Audiohook-Correlation-Id`` — all signed.

Reference: https://developer.genesys.cloud/devapps/audiohook/session-walkthrough#authentication

This module verifies the signature against a per-tenant client secret
read from ``Integration.provider_config["client_secret"]`` (encrypted
at rest, decrypted via ``services.token_crypto`` at lookup time). It
does NOT mint signatures — we never originate AudioHook calls.

What we deliberately don't enforce here:

* Replay protection beyond a coarse ``created`` timestamp window.
  Genesys recycles ``nonce`` values across sessions and the
  AppFoundry-listing review specifically tests for replay tolerance,
  so a strict nonce cache would cause false rejections. A future
  follow-up can add a Redis-backed nonce TTL once we have telemetry
  on real ``nonce`` reuse rates.
* ``alg`` parameter parsing. We pin to HMAC-SHA256 because that is
  the only algorithm AudioHook uses today; if Genesys ever rotates
  to a new algorithm the rotation will be a separate breaking change
  and we'd rather fail closed than silently accept an unknown alg.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


# Default ``created`` skew window, in seconds. Wider than typical
# (300s) because Genesys-side clock drift on AppFoundry test orgs has
# been observed up to a few minutes; tighten once production
# telemetry says otherwise.
DEFAULT_CREATED_SKEW_SECONDS = 600


class SignatureVerificationError(Exception):
    """Raised when the upgrade request fails HMAC verification.

    The server module catches this and rejects the upgrade with HTTP
    401 (or, for already-accepted WebSockets in test harnesses,
    closes with code 1008 — policy violation). The exception message
    is suitable for server-side logging but should NOT be returned
    in the response body — Genesys' troubleshooting docs ask
    integrations to return a generic error to discourage probing.
    """


# Components we ALWAYS require to be in the signature base when
# verifying an AudioHook upgrade. Genesys can choose to sign
# additional headers; absence of any of these is grounds for rejection.
REQUIRED_SIGNED_COMPONENTS: Tuple[str, ...] = (
    "@request-target",
    "@authority",
    "audiohook-organization-id",
    "audiohook-session-id",
    "audiohook-correlation-id",
    "x-api-key",
)


# ── Signature-Input parsing ─────────────────────────────────────────────


_QUOTED_LIST_RE = re.compile(r'"([^"]+)"')
_PARAM_RE = re.compile(r';\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*("([^"]*)"|([^\s;]+))')


@dataclass(frozen=True)
class SignatureInput:
    """Parsed ``Signature-Input`` header for one signature label.

    AudioHook only ever uses one label (``sig1``); we accept
    multi-label inputs but only verify the first one named in the
    ``Signature`` header.
    """

    label: str
    components: Tuple[str, ...]
    params: Mapping[str, str]

    @property
    def keyid(self) -> Optional[str]:
        return self.params.get("keyid")

    @property
    def alg(self) -> Optional[str]:
        return self.params.get("alg")

    @property
    def created(self) -> Optional[int]:
        raw = self.params.get("created")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None


def parse_signature_input(header: str) -> Dict[str, SignatureInput]:
    """Decode an RFC 9421 ``Signature-Input`` header value.

    The header looks like::

        sig1=("@request-target" "@authority" "x-api-key");keyid="...";alg="hmac-sha256";created=1700000000;nonce="abc"

    Multiple labels are comma-separated. We tolerate, but do not
    require, whitespace around commas and equals signs. Unparseable
    input returns an empty dict — the caller treats that as a
    verification failure.
    """

    out: Dict[str, SignatureInput] = {}
    if not header:
        return out
    # Split on commas that are NOT inside a quoted string. AudioHook
    # never embeds commas in the components list, so a simple split
    # on ``,`` works in practice; we still guard against the future
    # case where ``nonce`` contains a comma.
    for label_block in _split_top_level_commas(header):
        label_block = label_block.strip()
        if "=" not in label_block:
            continue
        label, _, rest = label_block.partition("=")
        label = label.strip()
        rest = rest.strip()
        if not rest.startswith("("):
            continue
        end = rest.find(")")
        if end < 0:
            continue
        components_block = rest[1:end]
        params_block = rest[end + 1 :]
        components = tuple(_QUOTED_LIST_RE.findall(components_block))
        params: Dict[str, str] = {}
        for match in _PARAM_RE.finditer(params_block):
            key = match.group(1)
            value = match.group(3) if match.group(3) is not None else match.group(4)
            params[key.lower()] = value
        out[label] = SignatureInput(label=label, components=components, params=params)
    return out


def _split_top_level_commas(s: str) -> List[str]:
    """Split on commas that are NOT inside a double-quoted run."""

    parts: List[str] = []
    buf: List[str] = []
    in_quotes = False
    for ch in s:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


# ── Signature header parsing ────────────────────────────────────────────


def parse_signature_header(header: str) -> Dict[str, bytes]:
    """Decode an RFC 9421 ``Signature`` header value.

    Format::

        sig1=:base64=:

    The leading and trailing colons mark a "byte sequence" sf type
    (RFC 8941). Multiple labels are comma-separated.
    """

    out: Dict[str, bytes] = {}
    if not header:
        return out
    for entry in _split_top_level_commas(header):
        entry = entry.strip()
        if "=" not in entry:
            continue
        label, _, raw_value = entry.partition("=")
        label = label.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith(":") and raw_value.endswith(":") and len(raw_value) >= 2:
            b64 = raw_value[1:-1]
            try:
                out[label] = base64.b64decode(b64, validate=True)
            except Exception:
                # Unparseable byte-sequence → caller treats as failure.
                continue
    return out


# ── Signature base construction ─────────────────────────────────────────


def build_signature_base(
    *,
    components: Iterable[str],
    method: str,
    target_path: str,
    authority: str,
    headers: Mapping[str, str],
    signature_input_value: str,
) -> bytes:
    """Construct the bytes-to-MAC for an RFC 9421 signature.

    Each component contributes one line of the form::

        "name": value\\n

    Followed by the signature parameters line::

        "@signature-params": (component-list);params

    Header lookups are case-insensitive. Missing required headers
    raise :class:`SignatureVerificationError` so the caller can
    return a single "bad signature" outcome rather than leaking which
    header was missing.
    """

    headers_ci = {k.lower(): v for k, v in headers.items()}
    lines: List[str] = []
    for comp in components:
        cl = comp.lower()
        if cl == "@request-target":
            # The AudioHook upgrade is a GET; @request-target is the
            # method (lowercased) + space + path-and-query per RFC 9421.
            lines.append(f'"{cl}": {method.lower()} {target_path}')
        elif cl == "@authority":
            lines.append(f'"{cl}": {authority}')
        elif cl == "@method":
            lines.append(f'"{cl}": {method.upper()}')
        else:
            value = headers_ci.get(cl)
            if value is None:
                raise SignatureVerificationError(
                    f"Signature base requires header {comp!r} which was not present"
                )
            # Trim per RFC 9421 §2.1.
            lines.append(f'"{cl}": {value.strip()}')
    lines.append(f'"@signature-params": {signature_input_value}')
    return "\n".join(lines).encode("utf-8")


# ── Top-level verification entrypoint ───────────────────────────────────


def verify_audiohook_signature(
    *,
    method: str,
    target_path: str,
    authority: str,
    headers: Mapping[str, str],
    client_secret: str,
    label: str = "sig1",
    skew_seconds: int = DEFAULT_CREATED_SKEW_SECONDS,
    now: Optional[int] = None,
) -> SignatureInput:
    """Verify the signature on an AudioHook WebSocket upgrade request.

    Returns the parsed :class:`SignatureInput` on success (the caller
    can use ``keyid`` to log which credential matched). Raises
    :class:`SignatureVerificationError` on any failure — missing
    headers, bad base64, algorithm mismatch, expired ``created``,
    HMAC inequality, or absence of a required component.

    ``client_secret`` is the per-tenant AudioHook integration secret
    (decrypted by the caller). Genesys derives the HMAC key directly
    from it (the secret is the key — there's no extra HKDF step in
    the AudioHook variant of the spec).

    The check is constant-time via ``hmac.compare_digest`` — short-
    circuit returns on missing components are NOT timing-attacked
    because they precede the secret-dependent branch.
    """

    sig_input_header = headers.get("Signature-Input") or headers.get("signature-input")
    sig_header = headers.get("Signature") or headers.get("signature")
    if not sig_input_header or not sig_header:
        raise SignatureVerificationError("Missing Signature or Signature-Input header")

    inputs = parse_signature_input(sig_input_header)
    if label not in inputs:
        raise SignatureVerificationError(
            f"Signature-Input has no label {label!r} (got: {sorted(inputs)})"
        )
    sig_input = inputs[label]

    sigs = parse_signature_header(sig_header)
    if label not in sigs:
        raise SignatureVerificationError(
            f"Signature header has no label {label!r} (got: {sorted(sigs)})"
        )
    provided_mac = sigs[label]

    # Algorithm pin — see module docstring.
    if sig_input.alg and sig_input.alg.lower() != "hmac-sha256":
        raise SignatureVerificationError(
            f"Unsupported signature alg: {sig_input.alg!r}"
        )

    # ``created`` skew — reject signatures from too far in the past
    # or future (clock skew tolerance). Absent ``created`` is allowed
    # because AudioHook makes the field optional.
    if sig_input.created is not None and skew_seconds > 0:
        current = int(now if now is not None else time.time())
        if abs(current - sig_input.created) > skew_seconds:
            raise SignatureVerificationError(
                f"Signature ``created`` outside skew window "
                f"(delta={current - sig_input.created}s, max={skew_seconds}s)"
            )

    # Required-component enforcement happens before we touch the
    # secret so a misshapen request fails fast.
    components_lower = tuple(c.lower() for c in sig_input.components)
    for required in REQUIRED_SIGNED_COMPONENTS:
        if required not in components_lower:
            raise SignatureVerificationError(
                f"Required component {required!r} missing from signature"
            )

    # Reconstruct the exact ``Signature-Input`` value-without-label
    # that Genesys signed over. RFC 9421 specifies this is the
    # ``label=...`` substring after the equals sign, including the
    # parens and parameters.
    sig_params_value = _extract_label_value(sig_input_header, label)
    base = build_signature_base(
        components=sig_input.components,
        method=method,
        target_path=target_path,
        authority=authority,
        headers=headers,
        signature_input_value=sig_params_value,
    )
    expected = hmac.new(
        client_secret.encode("utf-8"), base, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, provided_mac):
        raise SignatureVerificationError("HMAC mismatch")
    return sig_input


def _extract_label_value(signature_input_header: str, label: str) -> str:
    """Return the substring after ``label=`` in ``Signature-Input``.

    This is what RFC 9421 calls the "signature parameters value"
    — everything after the equals for the named label, up to the
    next top-level comma. Used as the right-hand side of the
    ``"@signature-params"`` line in the signature base.
    """

    for entry in _split_top_level_commas(signature_input_header):
        entry = entry.strip()
        if "=" not in entry:
            continue
        candidate, _, value = entry.partition("=")
        if candidate.strip() == label:
            return value.strip()
    raise SignatureVerificationError(
        f"Could not extract signature-params for label {label!r}"
    )
