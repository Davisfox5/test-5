"""End-to-end tests for the Zoom Phone UC adapter."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from tests.test_uc_common import (  # noqa: F401
    load_fixture,
    seeded_uc_integration,
    synthetic_mp3,
    uc_test_app,
    uc_test_client,
)

pytestmark = pytest.mark.asyncio

_SECRET = "zoom-fixture-secret-token"


def _sign(timestamp: str, body: bytes, secret: str = _SECRET) -> str:
    msg = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


async def test_zoom_url_validation_handshake(
    uc_test_client, test_tenant, seeded_uc_integration
):
    body = json.dumps(load_fixture("zoom_phone_url_validation.json")).encode()
    resp = await uc_test_client.post(
        f"/api/v1/uc/zoom/webhook/{test_tenant.id}",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["plainToken"] == "fixture-plain-token-abcdef0123456789"
    expected = hmac.new(
        _SECRET.encode(),
        b"fixture-plain-token-abcdef0123456789",
        hashlib.sha256,
    ).hexdigest()
    assert payload["encryptedToken"] == expected


async def test_zoom_verify_accepts_valid_signature():
    from backend.app.services.telephony.uc.zoom_phone import ZoomPhoneProvider

    body = json.dumps(load_fixture("zoom_phone_recording_completed.json")).encode()
    ts = "1746631200"
    provider = ZoomPhoneProvider()
    event = await provider.verify_webhook(
        headers={
            "x-zm-signature": _sign(ts, body),
            "x-zm-request-timestamp": ts,
        },
        body=body,
        tenant_secret=_SECRET,
    )
    assert event.provider == "zoom_phone"
    assert event.external_call_id == "call-fixture-zp-003"
    assert event.recording_id == "rec-fixture-zp-003-a"
    assert event.duration_seconds == 245
    assert event.recording_url == (
        "https://us04web.zoom.us/recording/download/rec-fixture-zp-003-a"
    )


async def test_zoom_verify_rejects_tampered_body():
    from backend.app.services.telephony.uc.base import WebhookVerificationError
    from backend.app.services.telephony.uc.zoom_phone import ZoomPhoneProvider

    body = json.dumps(load_fixture("zoom_phone_recording_completed.json")).encode()
    ts = "1746631200"
    sig = _sign(ts, body)
    tampered = body.replace(b"call-fixture-zp-003", b"attacker-call-id")
    provider = ZoomPhoneProvider()
    with pytest.raises(WebhookVerificationError):
        await provider.verify_webhook(
            headers={
                "x-zm-signature": sig,
                "x-zm-request-timestamp": ts,
            },
            body=tampered,
            tenant_secret=_SECRET,
        )


@respx.mock
async def test_zoom_full_pipeline(
    uc_test_client,
    test_tenant,
    test_session_factory,
    seeded_uc_integration,
    monkeypatch,
):
    from backend.app.api import uc_telephony as uc_module
    from backend.app.models import UcRecordingJob

    enqueued: list[str] = []
    monkeypatch.setattr(
        uc_module, "_enqueue_fetch", lambda jid: enqueued.append(str(jid))
    )

    body = json.dumps(load_fixture("zoom_phone_recording_completed.json")).encode()
    ts = "1746631200"
    sig = _sign(ts, body)
    resp = await uc_test_client.post(
        f"/api/v1/uc/zoom/webhook/{test_tenant.id}",
        headers={
            "x-zm-signature": sig,
            "x-zm-request-timestamp": ts,
        },
        content=body,
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert enqueued == [job_id]

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(UcRecordingJob).where(
                    UcRecordingJob.provider == "zoom_phone"
                )
            )
        ).scalar_one()
        assert job.external_call_id == "call-fixture-zp-003"
        assert job.recording_id == "rec-fixture-zp-003-a"

    from backend.app.services.telephony.uc.base import UCWebhookEvent
    from backend.app.services.telephony.uc.zoom_phone import ZoomPhoneProvider

    audio = synthetic_mp3()
    download_url = (
        "https://us04web.zoom.us/recording/download/rec-fixture-zp-003-a"
    )
    route = respx.get(download_url).mock(
        return_value=Response(
            200, content=audio, headers={"content-type": "audio/mp4"}
        )
    )

    event = UCWebhookEvent(
        provider="zoom_phone",
        external_call_id="call-fixture-zp-003",
        recording_id="rec-fixture-zp-003-a",
        recording_url=download_url,
        raw={"__provider_config": {}},
    )
    fetched = await ZoomPhoneProvider().fetch_recording(
        access_token="fixture-access-token-zoom_phone",
        event=event,
    )
    assert fetched.audio_bytes == audio
    assert fetched.content_type == "audio/mp4"

    assert route.called
    sent_auth = route.calls.last.request.headers.get("authorization")
    assert sent_auth == "Bearer fixture-access-token-zoom_phone"
