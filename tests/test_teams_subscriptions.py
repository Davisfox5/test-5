"""Tests for ``backend.app.services.teams_recording`` non-network code.

We exercise the parts that don't require a real Microsoft Graph
connection: subscription body construction, notification batch parsing,
the stub media bot, the PowerShell template, and the MSAL wrapper's
config-presence guard. The actual Graph HTTP call (``create_subscription``)
is intentionally not exercised here — that's a follow-on workstream
when the .NET media bot lands.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.services.teams_recording.bot_interface import (
    MediaBotNotDeployedError,
    MediaBotStatus,
    StubMediaBot,
    get_media_bot,
    reset_for_tests,
    set_media_bot_factory,
)
from backend.app.services.teams_recording.graph_app_auth import (
    GraphAppAuth,
    GraphAppAuthError,
    GraphToken,
)
from backend.app.services.teams_recording.policy import (
    CompliancePolicyTemplate,
    render_powershell,
)
from backend.app.services.teams_recording.subscriptions import (
    SUPPORTED_RESOURCES,
    SubscriptionSpec,
    SubscriptionValidationError,
    is_validation_handshake,
    parse_notifications,
    validation_response_body,
    _parse_iso8601,
)


FIXTURES = Path(__file__).parent / "fixtures" / "teams"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ── SubscriptionSpec.to_graph_body ───────────────────────────────────


def test_subscription_spec_renders_valid_body():
    spec = SubscriptionSpec(
        resource="communications/callRecords",
        notification_url="https://api.example.com/teams/notification",
        client_state="test-secret",
        lifetime_minutes=45,
    )
    body = spec.to_graph_body()
    assert body["resource"] == "communications/callRecords"
    assert body["notificationUrl"] == "https://api.example.com/teams/notification"
    assert body["clientState"] == "test-secret"
    assert body["changeType"] == "created,updated"
    # Expiration is a ISO-8601 string with millisecond precision and Z.
    assert body["expirationDateTime"].endswith("Z")
    parsed = datetime.strptime(
        body["expirationDateTime"], "%Y-%m-%dT%H:%M:%S.000Z"
    ).replace(tzinfo=timezone.utc)
    delta = parsed - datetime.now(timezone.utc)
    # Should be ~45 minutes out, allow generous slack for slow CI.
    assert timedelta(minutes=40) < delta < timedelta(minutes=50)


def test_subscription_spec_rejects_unsupported_resource():
    spec = SubscriptionSpec(
        resource="communications/calls/{id}",
        notification_url="https://api.example.com/teams/notification",
    )
    with pytest.raises(SubscriptionValidationError, match="SUPPORTED_RESOURCES"):
        spec.to_graph_body()


def test_subscription_spec_rejects_http_url():
    spec = SubscriptionSpec(
        resource="communications/callRecords",
        notification_url="http://insecure.example.com/teams",
    )
    with pytest.raises(SubscriptionValidationError, match="HTTPS"):
        spec.to_graph_body()


def test_supported_resources_contains_both_documented_endpoints():
    # If the plan ever broadens this list, the docs must be updated too.
    assert "communications/callRecords" in SUPPORTED_RESOURCES
    assert "communications/onlineMeetings/getAllRecordings" in SUPPORTED_RESOURCES


# ── Validation handshake helpers ─────────────────────────────────────


def test_validation_handshake_detected():
    assert is_validation_handshake({"validationToken": "abc"}) is True


def test_validation_handshake_not_detected_without_token():
    assert is_validation_handshake({"foo": "bar"}) is False


def test_validation_response_body_echoes_token():
    body = validation_response_body({"validationToken": "abc-xyz"})
    assert body == "abc-xyz"


def test_validation_response_body_rejects_empty_token():
    with pytest.raises(SubscriptionValidationError, match="non-empty"):
        validation_response_body({"validationToken": ""})


# ── parse_notifications ──────────────────────────────────────────────


def test_parse_call_record_notification_fixture():
    payload = _load("notification_call_record.json")
    notes = parse_notifications(
        payload, expected_client_state="scaffold-shared-secret"
    )
    assert len(notes) == 1
    note = notes[0]
    assert note.subscription_id == "11111111-2222-3333-4444-555555555555"
    assert note.change_type == "created"
    assert note.resource.startswith("communications/callRecords/")
    assert note.resource_data_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert note.tenant_id == "00000000-0000-0000-0000-000000000001"


def test_parse_recording_notification_fixture_multiple_entries():
    payload = _load("notification_recording_created.json")
    notes = parse_notifications(payload, expected_client_state="scaffold-shared-secret")
    assert len(notes) == 2
    assert {n.change_type for n in notes} == {"created", "updated"}
    assert all(
        n.resource == "communications/onlineMeetings/getAllRecordings" for n in notes
    )


def test_parse_rejects_bad_client_state():
    payload = _load("notification_bad_client_state.json")
    with pytest.raises(SubscriptionValidationError, match="clientState"):
        parse_notifications(payload, expected_client_state="scaffold-shared-secret")


def test_parse_skips_client_state_check_when_none():
    # The scaffold endpoint passes None because there's no per-subscription
    # state store yet — we should still parse the body.
    payload = _load("notification_bad_client_state.json")
    notes = parse_notifications(payload, expected_client_state=None)
    assert len(notes) == 1


def test_parse_rejects_non_object_payload():
    with pytest.raises(SubscriptionValidationError, match="JSON object"):
        parse_notifications([])  # type: ignore[arg-type]


def test_parse_rejects_missing_value_array():
    with pytest.raises(SubscriptionValidationError, match="value"):
        parse_notifications({"foo": "bar"})


def test_parse_rejects_entry_missing_fields():
    payload = {
        "value": [
            {"subscriptionId": "x", "changeType": "created"}  # no resource
        ]
    }
    with pytest.raises(SubscriptionValidationError, match="resource"):
        parse_notifications(payload)


# ── _parse_iso8601 ───────────────────────────────────────────────────


def test_parse_iso8601_handles_seven_digit_microseconds():
    dt = _parse_iso8601("2026-05-07T15:00:00.0000000Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 7


def test_parse_iso8601_returns_epoch_for_garbage():
    dt = _parse_iso8601("not-a-date")
    assert dt.year == 1970


def test_parse_iso8601_returns_epoch_for_none():
    dt = _parse_iso8601(None)
    assert dt.year == 1970


# ── GraphAppAuth (config presence + cache) ───────────────────────────


def test_graph_app_auth_unconfigured_by_default():
    # The test conftest sets DATABASE_URL etc. but not the Teams envs,
    # so a default GraphAppAuth must report not-configured.
    auth = GraphAppAuth(client_id="", client_secret="", tenant_id="")
    assert auth.is_configured() is False
    with pytest.raises(GraphAppAuthError, match="not configured"):
        auth.acquire_token()


def test_graph_app_auth_caches_token_until_expiry():
    auth = GraphAppAuth(
        client_id="cid", client_secret="csec", tenant_id="tid"
    )
    fake_token = GraphToken(
        access_token="abc", expires_at=time.time() + 1000, raw={}
    )
    with patch.object(auth, "_acquire_token", return_value=fake_token) as call:
        first = auth.acquire_token()
        second = auth.acquire_token()
    assert first.access_token == "abc"
    assert second is first
    # Second call must hit the cache, not _acquire_token.
    assert call.call_count == 1


def test_graph_app_auth_refreshes_after_expiry():
    auth = GraphAppAuth(
        client_id="cid", client_secret="csec", tenant_id="tid"
    )
    expired = GraphToken(
        access_token="old", expires_at=time.time() - 10, raw={}
    )
    fresh = GraphToken(
        access_token="new", expires_at=time.time() + 1000, raw={}
    )
    side_effect = iter([expired, fresh])
    with patch.object(
        auth, "_acquire_token", side_effect=lambda: next(side_effect)
    ):
        first = auth.acquire_token()
        assert first.access_token == "old"
        # Manually trigger expiry check by mutating _cached
        auth._cached.expires_at = time.time() - 10  # type: ignore[union-attr]
        second = auth.acquire_token()
        assert second.access_token == "new"


def test_graph_app_auth_authorization_header_format():
    auth = GraphAppAuth(
        client_id="cid", client_secret="csec", tenant_id="tid"
    )
    fake_token = GraphToken(
        access_token="bearer-abc", expires_at=time.time() + 1000, raw={}
    )
    with patch.object(auth, "_acquire_token", return_value=fake_token):
        header = auth.authorization_header()
    assert header == "Bearer bearer-abc"


# ── MediaBot stub ────────────────────────────────────────────────────


def test_default_media_bot_is_stub_and_reports_not_deployed():
    reset_for_tests()
    bot = get_media_bot()
    assert isinstance(bot, StubMediaBot)
    s = bot.status()
    assert s.deployed is False
    assert "not deployed" in s.reason.lower()
    assert bot.is_available() is False


def test_stub_bot_attach_raises_not_deployed():
    bot = StubMediaBot()
    with pytest.raises(MediaBotNotDeployedError):
        bot.attach_to_call("call-123")


def test_stub_bot_detach_is_noop():
    bot = StubMediaBot()
    # Must not raise.
    bot.detach("call-123")


def test_factory_can_be_swapped():
    reset_for_tests()

    class FakeBot(StubMediaBot):
        name = "fake"

        def status(self) -> MediaBotStatus:
            return MediaBotStatus(deployed=True, reason="fake-ok", bot_version="0.0.1")

        def is_available(self) -> bool:
            return True

    set_media_bot_factory(FakeBot)
    bot = get_media_bot()
    assert bot.name == "fake"
    assert bot.status().deployed is True
    reset_for_tests()
    assert isinstance(get_media_bot(), StubMediaBot)


# ── Compliance policy PowerShell template ───────────────────────────


def test_powershell_template_includes_bot_app_id_and_default_policy_name():
    template = CompliancePolicyTemplate(bot_app_id="appid-xyz")
    script = render_powershell(template)
    assert "appid-xyz" in script
    assert "LINDA-CompliancePolicy" in script
    assert "New-CsTeamsComplianceRecordingApplication" in script
    assert "New-CsTeamsComplianceRecordingPolicy" in script
    # No active grant block when no UPNs supplied (the header comment
    # mentions the cmdlet as an example, but the optional section is omitted).
    assert "# 4. (Optional)" not in script


def test_powershell_template_includes_grants_when_upns_supplied():
    template = CompliancePolicyTemplate(
        bot_app_id="appid-xyz",
        target_user_upns=["alice@example.com", "bob@example.com"],
    )
    script = render_powershell(template)
    assert "Grant-CsTeamsComplianceRecordingPolicy" in script
    assert "alice@example.com" in script
    assert "bob@example.com" in script


def test_powershell_template_requires_bot_app_id():
    template = CompliancePolicyTemplate(bot_app_id="")
    with pytest.raises(ValueError, match="bot_app_id"):
        render_powershell(template)


# ── Provider Literal contract ────────────────────────────────────────


def test_teams_compliance_is_in_telephony_provider_literal():
    """If this fails, Stream 0's namespace contract has drifted from
    the plan. Coordinate via the plan doc — don't silently extend."""

    import typing

    from backend.app.services.telephony import TelephonyProvider

    assert "teams_compliance" in typing.get_args(TelephonyProvider)
