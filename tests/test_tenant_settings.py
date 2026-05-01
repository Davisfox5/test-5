"""Tests for the tenant retention overrides surface on the admin
``/admin/tenant-settings`` PATCH.

Covers update / clear-override / 403 (non-admin gating).

The retention overrides ride on the existing tenant-settings endpoint —
admins can flip ``audio_retention_hours_override`` and
``feedback_retention_days_override`` to opt in to a different cadence
on the nightly retention sweep. Sending ``null`` clears the override.

The PATCH handler is admin-gated at the router include level (see
``backend/app/main.py``), so we exercise the role gate separately by
calling ``require_role("admin")`` directly with a non-admin principal —
matching the gating contract without booting the full app.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.app.api.admin import (
    TenantSettingsPatch,
    _tenant_settings_payload,
    patch_tenant_settings,
)
from backend.app.auth import AuthPrincipal, require_role


# ── Direct handler tests ──────────────────────────────────────────────


class _MutTenant:
    """Minimal tenant stand-in matching the columns the handler touches."""

    def __init__(self, **overrides):
        defaults = dict(
            id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            transcription_engine="deepgram",
            automation_level="approval",
            pii_redaction_enabled=True,
            translation_enabled=False,
            default_language="en",
            keyterm_boost_list=[],
            question_keyterms=[],
            features_enabled={},
            plan_tier="sandbox",
            seat_limit=3,
            admin_seat_limit=1,
            audio_retention_hours=168,
            retention_days_feedback_events=None,
            retention_days_webhook_deliveries=None,
        )
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(self, k, v)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_payload_exposes_retention_defaults_and_current_values():
    tenant = _MutTenant(audio_retention_hours=240, retention_days_feedback_events=90)
    payload = _tenant_settings_payload(tenant)  # type: ignore[arg-type]
    assert payload["audio_retention_hours"] == 240
    assert payload["audio_retention_hours_default"] == 168
    assert payload["feedback_retention_days_override"] == 90
    # Default sourced from event_retention.FEEDBACK_EVENT_RAW_RETENTION_DAYS.
    assert payload["feedback_retention_days_default"] == 365


def test_patch_updates_retention_overrides():
    tenant = _MutTenant()
    body = TenantSettingsPatch(
        audio_retention_hours_override=72,
        feedback_retention_days_override=180,
    )
    out = asyncio.new_event_loop().run_until_complete(
        patch_tenant_settings(body, tenant)  # type: ignore[arg-type]
    )
    assert tenant.audio_retention_hours == 72
    assert tenant.retention_days_feedback_events == 180
    assert out["audio_retention_hours"] == 72
    assert out["feedback_retention_days_override"] == 180


def test_patch_clear_override_with_null():
    """PATCH'ing the override fields to ``None`` clears them — audio falls
    back to the platform default (168), feedback returns to ``null`` so
    the sweep uses the global default."""
    tenant = _MutTenant(audio_retention_hours=72, retention_days_feedback_events=180)
    body = TenantSettingsPatch(
        audio_retention_hours_override=None,
        feedback_retention_days_override=None,
    )
    asyncio.new_event_loop().run_until_complete(
        patch_tenant_settings(body, tenant)  # type: ignore[arg-type]
    )
    assert tenant.audio_retention_hours == 168
    assert tenant.retention_days_feedback_events is None


def test_patch_omitting_retention_fields_leaves_them_alone():
    """A PATCH that flips a feature flag must not zero retention values."""
    tenant = _MutTenant(audio_retention_hours=72, retention_days_feedback_events=180)
    body = TenantSettingsPatch(transcription_engine="whisper")
    asyncio.new_event_loop().run_until_complete(
        patch_tenant_settings(body, tenant)  # type: ignore[arg-type]
    )
    assert tenant.transcription_engine == "whisper"
    assert tenant.audio_retention_hours == 72
    assert tenant.retention_days_feedback_events == 180


def test_patch_rejects_out_of_range_audio_hours():
    tenant = _MutTenant()
    body = TenantSettingsPatch(audio_retention_hours_override=0)
    with pytest.raises(Exception) as exc:
        asyncio.new_event_loop().run_until_complete(
            patch_tenant_settings(body, tenant)  # type: ignore[arg-type]
        )
    # FastAPI HTTPException — surfaces a 400 with the validation message.
    assert "audio_retention_hours_override" in str(exc.value)


def test_patch_rejects_out_of_range_feedback_days():
    tenant = _MutTenant()
    body = TenantSettingsPatch(feedback_retention_days_override=10_000)
    with pytest.raises(Exception) as exc:
        asyncio.new_event_loop().run_until_complete(
            patch_tenant_settings(body, tenant)  # type: ignore[arg-type]
        )
    assert "feedback_retention_days_override" in str(exc.value)


# ── Role gate ────────────────────────────────────────────────────────


def test_require_admin_rejects_non_admin_principal():
    """The admin router is gated by ``require_role("admin")`` at the
    include level. A manager / agent principal must 403."""
    dep = require_role("admin")
    fake_principal = AuthPrincipal(
        tenant=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), role="agent"),  # type: ignore[arg-type]
        role="agent",
        source="session",
    )
    with pytest.raises(Exception) as exc:
        asyncio.new_event_loop().run_until_complete(dep(fake_principal))
    assert "403" in str(exc.value) or "Requires role" in str(exc.value)


def test_require_admin_passes_admin_principal():
    dep = require_role("admin")
    admin_principal = AuthPrincipal(
        tenant=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        user=SimpleNamespace(id=uuid.uuid4(), role="admin"),  # type: ignore[arg-type]
        role="admin",
        source="session",
    )
    out = asyncio.new_event_loop().run_until_complete(dep(admin_principal))
    assert out is admin_principal
