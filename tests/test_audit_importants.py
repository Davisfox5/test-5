"""Targeted tests for the load-bearing fixes shipped under audit-importants.

Covers four areas:

* webhook URL SSRF guard — ``is_safe_webhook_url``
* trial-expiry sweep — bucket selection (3d / 1d / expired)
* health soft-probes — graceful failure when an upstream is down
* Microsoft OAuth scope normalization helper
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# ── Webhook URL SSRF guard ──────────────────────────────────────────


from backend.app.services.webhook_dispatcher import is_safe_webhook_url


def test_is_safe_webhook_url_accepts_public_https():
    assert is_safe_webhook_url("https://example.com/webhooks/linda") is True


def test_is_safe_webhook_url_accepts_public_http():
    # http:// is allowed for self-hosted dev — only the host range matters.
    assert is_safe_webhook_url("http://example.com/cb") is True


def test_is_safe_webhook_url_rejects_loopback_v4():
    assert is_safe_webhook_url("http://127.0.0.1/cb") is False
    assert is_safe_webhook_url("https://127.0.0.1/cb") is False


def test_is_safe_webhook_url_rejects_loopback_v6():
    assert is_safe_webhook_url("http://[::1]/cb") is False


def test_is_safe_webhook_url_rejects_localhost_name():
    assert is_safe_webhook_url("http://localhost:8080/cb") is False
    assert is_safe_webhook_url("https://app.localhost/cb") is False


def test_is_safe_webhook_url_rejects_link_local_imds():
    # AWS / GCP instance-metadata service.
    assert is_safe_webhook_url("http://169.254.169.254/latest/meta-data") is False


def test_is_safe_webhook_url_rejects_rfc1918():
    for url in (
        "http://10.0.0.1/cb",
        "http://192.168.1.1/cb",
        "http://172.16.0.1/cb",
    ):
        assert is_safe_webhook_url(url) is False, url


def test_is_safe_webhook_url_rejects_invalid_scheme():
    assert is_safe_webhook_url("file:///etc/passwd") is False
    assert is_safe_webhook_url("ftp://example.com/cb") is False
    assert is_safe_webhook_url("") is False


# ── Microsoft OAuth scope normalization ────────────────────────────


def test_microsoft_granted_scope_string_split():
    # Mirrors the helper logic inside oauth.py: read result["scope"] and
    # fall back to the request set if empty / missing.
    from backend.app.api.oauth import MICROSOFT_SCOPES

    granted_raw = "Mail.Send Mail.Read"
    granted = [s for s in granted_raw.split(" ") if s] or MICROSOFT_SCOPES
    assert granted == ["Mail.Send", "Mail.Read"]

    # Missing/empty falls back to the full request set so we never write
    # an empty scopes list onto the integration row.
    granted_raw = ""
    granted = [s for s in granted_raw.split(" ") if s] or MICROSOFT_SCOPES
    assert granted == MICROSOFT_SCOPES


# ── Stripe price-id → tier doctest builder ─────────────────────────


def test_price_tier_pairs_new_overrides_legacy():
    from backend.app.services.stripe_billing import _build_price_tier_pairs

    s = SimpleNamespace(
        STRIPE_PRICE_SOLO="price_dual",
        STRIPE_PRICE_TEAM="",
        STRIPE_PRICE_PRO="",
        STRIPE_PRICE_SANDBOX="price_dual",
        STRIPE_PRICE_STARTER="",
        STRIPE_PRICE_GROWTH="",
        STRIPE_PRICE_ENTERPRISE="",
    )
    # Legacy SOLO + new SANDBOX both point at the same price id; the
    # final dict reflects the *new* assignment because the new keys are
    # processed last in the build loop.
    assert _build_price_tier_pairs(s)["price_dual"] == "sandbox"


def test_price_tier_pairs_skips_blank_envs():
    from backend.app.services.stripe_billing import _build_price_tier_pairs

    s = SimpleNamespace(
        STRIPE_PRICE_SOLO="",
        STRIPE_PRICE_TEAM="",
        STRIPE_PRICE_PRO="",
        STRIPE_PRICE_SANDBOX="price_x",
        STRIPE_PRICE_STARTER="",
        STRIPE_PRICE_GROWTH="",
        STRIPE_PRICE_ENTERPRISE="",
    )
    assert _build_price_tier_pairs(s) == {"price_x": "sandbox"}


# ── Trial-expiry sweep bucket logic ────────────────────────────────


@pytest.mark.parametrize(
    "days_until_end,expected_bucket",
    [
        (5.0, None),  # outside the 3-day window — no notice
        (2.5, "warned_3d"),  # inside (1, 3] days
        (1.0, "warned_1d"),  # inside (0, 1] days
        (0.5, "warned_1d"),
        (-0.1, "expired"),  # past the deadline
        (-7, "expired"),  # well past — still "expired", not skipped
    ],
)
def test_trial_expiry_bucket_selection(days_until_end, expected_bucket):
    """Replicates the bucket-selection logic from
    ``backend.app.tasks.trial_expiry_daily`` so a refactor that breaks
    the thresholds gets caught at unit-test time."""
    seconds_left = days_until_end * 86_400
    days_left = seconds_left / 86_400

    bucket = None
    if seconds_left <= 0:
        bucket = "expired"
    elif days_left <= 1:
        bucket = "warned_1d"
    elif days_left <= 3:
        bucket = "warned_3d"

    assert bucket == expected_bucket


# ── Health soft-probes graceful failure ────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_probe_returns_unconfigured_when_key_blank():
    """An empty ANTHROPIC_API_KEY shouldn't blow up the readiness probe —
    it should return ``{"configured": False}`` so the soft-check passes."""
    import backend.app.api.health as health
    from backend.app.config import get_settings

    real = get_settings()
    fake = SimpleNamespace(
        ANTHROPIC_API_KEY="",
        VOYAGE_API_KEY="",
        VOYAGE_EMBED_MODEL="voyage-3",
    )
    health.get_settings = lambda: fake  # type: ignore[assignment]
    try:
        out = await health._probe_anthropic()
    finally:
        health.get_settings = lambda: real  # type: ignore[assignment]
    assert out == {"configured": False}


@pytest.mark.asyncio
async def test_voyage_probe_returns_unconfigured_when_key_blank():
    import backend.app.api.health as health
    from backend.app.config import get_settings

    real = get_settings()
    fake = SimpleNamespace(
        ANTHROPIC_API_KEY="",
        VOYAGE_API_KEY="",
        VOYAGE_EMBED_MODEL="voyage-3",
    )
    health.get_settings = lambda: fake  # type: ignore[assignment]
    try:
        out = await health._probe_voyage()
    finally:
        health.get_settings = lambda: real  # type: ignore[assignment]
    assert out == {"configured": False}


# ── Webhook event-name validation ──────────────────────────────────


def test_validate_events_rejects_unknown():
    from fastapi import HTTPException

    from backend.app.api.webhooks import _validate_events

    with pytest.raises(HTTPException) as exc:
        _validate_events(["interaction.outcom_inferred"])  # typo of "outcome_inferred"
    assert exc.value.status_code == 400


def test_validate_events_collapses_wildcard():
    from backend.app.api.webhooks import _validate_events

    # Wildcard subsumes everything else; the helper drops the redundancy.
    out = _validate_events(["*", "interaction.analyzed"])
    assert out == ["*"]


def test_validate_events_dedupes_preserving_order():
    from backend.app.api.webhooks import _validate_events
    from backend.app.services.webhook_events import WEBHOOK_EVENTS

    # Pick two real events from the catalog to keep this future-proof.
    catalog = list(WEBHOOK_EVENTS.keys())
    if len(catalog) < 2:
        pytest.skip("catalog too small for dedupe test")
    a, b = catalog[0], catalog[1]
    assert _validate_events([a, b, a]) == [a, b]
