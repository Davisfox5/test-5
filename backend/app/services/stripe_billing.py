"""Stripe billing integration.

Two pure surfaces here — the API handler in ``api/stripe_webhook.py``
composes them with the database.

* ``verify_stripe_signature(payload_bytes, header, secret, tolerance_s)``
  — verifies Stripe's ``Stripe-Signature`` header. Format:
  ``t=<timestamp>,v1=<hex>[,v1=<hex>]`` where each ``v1`` is
  ``HMAC_SHA256(secret, "{t}.{raw_body}")``. Stripe can send multiple
  ``v1`` values during key rotation — we accept if any matches.
* ``price_id_to_tier(price_id)`` — maps a Stripe price_id back to a
  tier key (``solo|team|pro|enterprise``). Unknown → None. Based on
  the ``STRIPE_PRICE_*`` env settings so the mapping is deployable
  without code changes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Dict, Optional

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


# Webhook age limit in seconds. Stripe suggests 5 minutes; too short
# and legitimate retries get rejected, too long and replay attacks get
# easier. 300 matches Stripe's own default.
DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300


def verify_stripe_signature(
    *,
    payload_bytes: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Return True when the header validates against the payload.

    Stripe header format::

        t=1677712346,v1=0a1b2c…,v1=9f8e7d…

    Multiple ``v1`` entries mean key-rotation is in progress; we accept
    if the payload matches any of them.
    """
    if not (payload_bytes is not None and signature_header and secret):
        return False

    parsed = _parse_signature_header(signature_header)
    if parsed is None:
        return False
    timestamp, v1_signatures = parsed

    # Replay protection: reject timestamps outside the tolerance window.
    current = now if now is not None else time.time()
    if abs(current - timestamp) > tolerance_seconds:
        return False

    signed_payload = f"{timestamp}.".encode("utf-8") + payload_bytes
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    for candidate in v1_signatures:
        if hmac.compare_digest(expected, candidate.strip()):
            return True
    return False


def _parse_signature_header(header: str) -> Optional[tuple[int, list[str]]]:
    """Return (timestamp, [v1_sigs]) or None if the header is malformed."""
    timestamp: Optional[int] = None
    v1: list[str] = []
    for chunk in header.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None
        elif key == "v1":
            v1.append(value)
    if timestamp is None or not v1:
        return None
    return timestamp, v1


# ── Price → tier mapping ─────────────────────────────────────────────


_PRICE_TIER_CACHE: Dict[str, Dict[str, str]] = {}


def _price_tier_pairs() -> Dict[str, str]:
    """Return the current env price_id → plan tier key mapping.

    Prefers the new ``STRIPE_PRICE_{SANDBOX,STARTER,GROWTH,ENTERPRISE}``
    env vars. Legacy ``STRIPE_PRICE_{SOLO,TEAM,PRO}`` vars are honored
    as aliases so an existing deployment can roll the rename at its own
    pace; when both new and legacy are set the new var wins (the new
    keys are inserted last in the loop, so they overwrite any legacy
    alias that happens to share a price_id).

    Memoized per (settings-id) — settings is a singleton in production
    so the dict is materialized once per process; tests that monkey-patch
    ``get_settings()`` re-key on the new instance and rebuild.

    Precedence example::

        >>> from types import SimpleNamespace
        >>> s = SimpleNamespace(
        ...     STRIPE_PRICE_SOLO="price_legacy_solo",
        ...     STRIPE_PRICE_TEAM="",
        ...     STRIPE_PRICE_PRO="",
        ...     STRIPE_PRICE_SANDBOX="price_new_sandbox",
        ...     STRIPE_PRICE_STARTER="",
        ...     STRIPE_PRICE_GROWTH="",
        ...     STRIPE_PRICE_ENTERPRISE="",
        ... )
        >>> _build_price_tier_pairs(s) == {
        ...     "price_legacy_solo": "sandbox",
        ...     "price_new_sandbox": "sandbox",
        ... }
        True

        # When new + legacy share a price_id, the new entry wins (the dict
        # ends up with the new var's tier even if both pointed at it):
        >>> s2 = SimpleNamespace(
        ...     STRIPE_PRICE_SOLO="price_dual",
        ...     STRIPE_PRICE_TEAM="",
        ...     STRIPE_PRICE_PRO="",
        ...     STRIPE_PRICE_SANDBOX="price_dual",
        ...     STRIPE_PRICE_STARTER="",
        ...     STRIPE_PRICE_GROWTH="",
        ...     STRIPE_PRICE_ENTERPRISE="",
        ... )
        >>> _build_price_tier_pairs(s2)["price_dual"]
        'sandbox'
    """
    settings = get_settings()
    cache_key = str(id(settings))
    cached = _PRICE_TIER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    pairs = _build_price_tier_pairs(settings)
    _PRICE_TIER_CACHE[cache_key] = pairs
    return pairs


def _build_price_tier_pairs(settings) -> Dict[str, str]:
    """Pure builder — exposed for unit tests + doctests."""
    pairs: Dict[str, str] = {}
    # Legacy first, then new — so new entries overwrite when both exist.
    for price_id, tier in [
        (settings.STRIPE_PRICE_SOLO, "sandbox"),
        (settings.STRIPE_PRICE_TEAM, "starter"),
        (settings.STRIPE_PRICE_PRO, "growth"),
        (settings.STRIPE_PRICE_SANDBOX, "sandbox"),
        (settings.STRIPE_PRICE_STARTER, "starter"),
        (settings.STRIPE_PRICE_GROWTH, "growth"),
        (settings.STRIPE_PRICE_ENTERPRISE, "enterprise"),
    ]:
        if price_id:
            pairs[price_id] = tier
    return pairs


def price_id_to_tier(price_id: str) -> Optional[str]:
    """Translate a Stripe price_id into one of our plan tier keys.

    Driven by the ``STRIPE_PRICE_*`` env settings so a deployment can
    remap tiers without a code change. Returns ``None`` for unknown or
    blank price_ids.
    """
    if not price_id:
        return None
    return _price_tier_pairs().get(price_id)


def price_tier_map_for_api() -> Dict[str, str]:
    """Return the configured mapping for the admin UI."""
    return _price_tier_pairs()
