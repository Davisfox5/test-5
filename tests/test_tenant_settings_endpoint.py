"""Smoke tests for the tenant settings PATCH payload logic.

We don't boot the whole FastAPI TestClient here — the admin router depends
on a live Postgres. Instead we exercise the payload builder + validator
directly, since those are the interesting parts (allowlists, merge
semantics). The live round-trip is covered in the Playwright suite.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.api.admin import (
    _FEATURE_FLAG_SPEC,
    TenantSettingsPatch,
    _tenant_settings_payload,
)


def _fake_tenant(**overrides):
    defaults = dict(
        id="11111111-1111-1111-1111-111111111111",
        transcription_engine="deepgram",
        automation_level="approval",
        pii_redaction_enabled=True,
        audio_storage_enabled=False,
        translation_enabled=False,
        default_language="en",
        keyterm_boost_list=[],
        question_keyterms=[],
        features_enabled={"live_sentiment": True},
        # Plan surface the payload now exposes.
        plan_tier="sandbox",
        seat_limit=3,
        admin_seat_limit=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_payload_merges_spec_defaults_into_features():
    tenant = _fake_tenant(features_enabled={"live_sentiment": True})
    payload = _tenant_settings_payload(tenant)
    # Spec lists 5 flags — all should appear in the response.
    spec_keys = {s["key"] for s in _FEATURE_FLAG_SPEC}
    assert spec_keys.issubset(payload["features_enabled"].keys())
    # Explicit value from the tenant survives.
    assert payload["features_enabled"]["live_sentiment"] is True
    # Missing flag gets its spec default (live_kb_retrieval defaults True).
    assert payload["features_enabled"]["live_kb_retrieval"] is True


def test_payload_exposes_feature_flag_spec():
    tenant = _fake_tenant()
    payload = _tenant_settings_payload(tenant)
    assert isinstance(payload["feature_flag_spec"], list)
    assert all({"key", "default", "label", "help"}.issubset(s) for s in payload["feature_flag_spec"])


def test_patch_model_drops_unspecified_fields():
    """A PATCH with only features_enabled should serialise cleanly."""
    body = TenantSettingsPatch(features_enabled={"live_sentiment": False})
    dumped = body.model_dump(exclude_none=True)
    assert dumped == {"features_enabled": {"live_sentiment": False}}


def test_patch_model_accepts_multiple_fields():
    body = TenantSettingsPatch(
        transcription_engine="whisper",
        default_language="fr",
        keyterm_boost_list=["acme", "refund policy"],
    )
    dumped = body.model_dump(exclude_none=True)
    assert dumped["transcription_engine"] == "whisper"
    assert dumped["default_language"] == "fr"
    assert dumped["keyterm_boost_list"] == ["acme", "refund policy"]
    assert "features_enabled" not in dumped
