"""Tests for OAuth post-connect return_to + the api-key SPA-skip.

External consoles embed LINDA's OAuth and need the user returned to *their*
app, not bounced into LINDA's SPA. ``return_to`` carries that target; it is
allowlist-validated to prevent open redirects, and api-key-initiated flows
don't get an SPA redirect.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.responses import RedirectResponse

from backend.app.api import oauth


def _settings(**over):
    base = dict(
        ALLOWED_ORIGINS=["https://linda-staging-app.fly.dev"],
        OAUTH_RETURN_TO_ALLOWED_ORIGINS=["https://console.example.com"],
        SPA_URL="https://linda-staging-app.fly.dev",
        DEBUG=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── _validate_return_to ─────────────────────────────────


def test_validate_return_to_accepts_allowlisted_console():
    with patch.object(oauth, "settings", _settings()):
        assert (
            oauth._validate_return_to("https://console.example.com/integrations?x=1")
            == "https://console.example.com/integrations?x=1"
        )


def test_validate_return_to_accepts_spa_and_allowed_origins():
    with patch.object(oauth, "settings", _settings()):
        assert oauth._validate_return_to(
            "https://linda-staging-app.fly.dev/settings"
        )


def test_validate_return_to_rejects_unknown_origin():
    with patch.object(oauth, "settings", _settings()):
        assert oauth._validate_return_to("https://evil.example.com/grab") is None


def test_validate_return_to_rejects_relative_and_scheme_tricks():
    with patch.object(oauth, "settings", _settings()):
        assert oauth._validate_return_to("/settings") is None          # relative
        assert oauth._validate_return_to("javascript:alert(1)") is None
        assert oauth._validate_return_to("") is None
        assert oauth._validate_return_to(None) is None


def test_validate_return_to_http_only_localhost_in_debug():
    # Plain http rejected in prod...
    with patch.object(oauth, "settings", _settings()):
        assert oauth._validate_return_to("http://localhost:3000/cb") is None
    # ...allowed for localhost when DEBUG.
    with patch.object(
        oauth,
        "settings",
        _settings(DEBUG=True, OAUTH_RETURN_TO_ALLOWED_ORIGINS=["http://localhost:3000"]),
    ):
        assert (
            oauth._validate_return_to("http://localhost:3000/cb")
            == "http://localhost:3000/cb"
        )


# ── _finish_connect ─────────────────────────────────────


def _default_marker():
    return {"status": "connected", "provider": "google"}


def test_finish_connect_prefers_return_to():
    with patch.object(oauth, "settings", _settings()):
        resp = oauth._finish_connect(
            "google",
            {"return_to": "https://console.example.com/done", "source": "api_key"},
            default=_default_marker,
        )
    assert isinstance(resp, RedirectResponse)
    assert resp.headers["location"] == (
        "https://console.example.com/done?integration_connected=google"
    )


def test_finish_connect_appends_with_ampersand_when_query_present():
    with patch.object(oauth, "settings", _settings()):
        resp = oauth._finish_connect(
            "google",
            {"return_to": "https://console.example.com/done?ref=1"},
            default=_default_marker,
        )
    assert resp.headers["location"].endswith("?ref=1&integration_connected=google")


def test_finish_connect_api_key_skips_spa():
    # api-key flow, no return_to → JSON, NOT an SPA redirect.
    with patch.object(oauth, "settings", _settings()):
        out = oauth._finish_connect(
            "google", {"source": "api_key"}, default=_default_marker
        )
    assert out == {"status": "connected", "provider": "google"}


def test_finish_connect_falls_back_to_default():
    # Interactive (session) flow, no return_to → provider default runs.
    sentinel = object()
    with patch.object(oauth, "settings", _settings()):
        out = oauth._finish_connect(
            "google", {"source": "session"}, default=lambda: sentinel
        )
    assert out is sentinel


def test_finish_connect_rejected_return_to_falls_through():
    # A non-allowlisted return_to must not redirect — falls to default.
    sentinel = object()
    with patch.object(oauth, "settings", _settings()):
        out = oauth._finish_connect(
            "google",
            {"return_to": "https://evil.example.com/x", "source": "session"},
            default=lambda: sentinel,
        )
    assert out is sentinel
