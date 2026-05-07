"""Smoke tests for the OAuth registry rename + UC vendor entries."""

from __future__ import annotations


def test_back_compat_alias_still_imports():
    """Old name continues to work for callers that already imported it."""
    from backend.app.api.oauth import CRM_PROVIDERS, OAUTH_PROVIDERS

    assert CRM_PROVIDERS is OAUTH_PROVIDERS  # alias, not a copy


def test_uc_vendors_registered_with_correct_shape():
    from backend.app.api.oauth import OAUTH_PROVIDERS

    for name in ("ringcentral", "webex_calling", "zoom_phone"):
        assert name in OAUTH_PROVIDERS, f"{name} missing from OAUTH_PROVIDERS"
        spec = OAUTH_PROVIDERS[name]
        for required in (
            "authorize_url",
            "token_url",
            "scopes",
            "scope_sep",
            "client_id_key",
            "client_secret_key",
        ):
            assert required in spec, f"{name} missing {required!r}"
        assert spec.get("certified") is False, f"{name} should be certified=False"


def test_uc_vendors_in_supported_providers_set():
    from backend.app.api.oauth import SUPPORTED_PROVIDERS

    for name in ("ringcentral", "webex_calling", "zoom_phone"):
        assert name in SUPPORTED_PROVIDERS, f"{name} not in SUPPORTED_PROVIDERS"


def test_webex_uses_pkce():
    from backend.app.api.oauth import OAUTH_PROVIDERS

    assert OAUTH_PROVIDERS["webex_calling"].get("use_pkce") is True
    assert OAUTH_PROVIDERS["ringcentral"].get("use_pkce") in (None, False)
    assert OAUTH_PROVIDERS["zoom_phone"].get("use_pkce") in (None, False)


def test_uc_provider_strings_are_in_telephony_literal():
    """Every Integration.provider write must come from the typed Literal."""
    import typing

    from backend.app.services.telephony import TelephonyProvider

    args = typing.get_args(TelephonyProvider)
    for name in ("ringcentral", "webex_calling", "zoom_phone"):
        assert name in args, f"{name} missing from TelephonyProvider"


def test_oauth_providers_endpoint_lists_uc_vendors():
    """``GET /oauth/providers`` enumerates every catalog entry."""
    import asyncio

    from backend.app.api.oauth import oauth_providers

    response = asyncio.run(oauth_providers())
    names = {p.provider for p in response.providers}
    for name in ("ringcentral", "webex_calling", "zoom_phone"):
        assert name in names, f"{name} not in /oauth/providers response"
