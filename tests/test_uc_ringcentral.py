"""End-to-end tests for the RingCentral UC adapter."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from tests.test_uc_common import (  # noqa: F401 — fixture re-export
    load_fixture,
    seeded_uc_integration,
    synthetic_mp3,
    uc_test_app,
    uc_test_client,
)

pytestmark = pytest.mark.asyncio


async def test_ringcentral_verify_accepts_valid_token():
    from backend.app.services.telephony.uc.ringcentral import RingCentralProvider

    body = json.dumps(
        load_fixture("ringcentral_telephony_session.json")
    ).encode()
    provider = RingCentralProvider()
    event = await provider.verify_webhook(
        headers={"verification-token": "rc-fixture-verification-token"},
        body=body,
        signing_secret="rc-fixture-verification-token",
    )
    assert event.provider == "ringcentral"
    assert event.external_call_id == "s-fixture-rc-001"
    assert event.recording_id == "rec-fixture-001"
    assert event.recording_url and "rec-fixture-001/content" in event.recording_url
    assert event.duration_seconds == 187
    assert event.direction == "inbound"


async def test_ringcentral_verify_rejects_wrong_token():
    from backend.app.services.telephony.uc.base import WebhookVerificationError
    from backend.app.services.telephony.uc.ringcentral import RingCentralProvider

    body = json.dumps(
        load_fixture("ringcentral_telephony_session.json")
    ).encode()
    provider = RingCentralProvider()
    with pytest.raises(WebhookVerificationError):
        await provider.verify_webhook(
            headers={"verification-token": "WRONG"},
            body=body,
            signing_secret="rc-fixture-verification-token",
        )


async def test_ringcentral_verify_rejects_missing_header():
    from backend.app.services.telephony.uc.base import WebhookVerificationError
    from backend.app.services.telephony.uc.ringcentral import RingCentralProvider

    provider = RingCentralProvider()
    with pytest.raises(WebhookVerificationError):
        await provider.verify_webhook(
            headers={},
            body=b'{"foo":"bar"}',
            signing_secret="rc-fixture-verification-token",
        )


async def test_ringcentral_validation_token_handshake(
    uc_test_client, test_tenant, seeded_uc_integration
):
    resp = await uc_test_client.post(
        f"/api/v1/uc/ringcentral/webhook/{test_tenant.id}",
        headers={"validation-token": "vt-handshake-abc"},
        content=b"",
    )
    assert resp.status_code == 200
    assert resp.headers.get("validation-token") == "vt-handshake-abc"


@respx.mock
async def test_ringcentral_full_pipeline(
    uc_test_client,
    test_tenant,
    test_session_factory,
    seeded_uc_integration,
    monkeypatch,
):
    """Webhook → UcRecordingJob → fetch_recording happy path."""
    from backend.app.api import uc_telephony as uc_module
    from backend.app.models import UcRecordingJob

    enqueued: list[str] = []
    monkeypatch.setattr(
        uc_module, "_enqueue_fetch", lambda jid: enqueued.append(str(jid))
    )

    body = json.dumps(
        load_fixture("ringcentral_telephony_session.json")
    ).encode()
    resp = await uc_test_client.post(
        f"/api/v1/uc/ringcentral/webhook/{test_tenant.id}",
        headers={"verification-token": "rc-fixture-verification-token"},
        content=body,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "queued"
    job_id = payload["job_id"]
    assert enqueued == [job_id]

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(UcRecordingJob).where(
                    UcRecordingJob.provider == "ringcentral"
                )
            )
        ).scalar_one()
        assert str(job.id) == job_id
        assert job.external_call_id == "s-fixture-rc-001"
        assert job.recording_id == "rec-fixture-001"
        assert job.state == "pending"
        assert "__provider_config" not in (job.payload or {})

    from backend.app.services.telephony.uc.base import UCWebhookEvent
    from backend.app.services.telephony.uc.ringcentral import RingCentralProvider

    audio = synthetic_mp3()
    respx.get(
        "https://media.ringcentral.com/restapi/v1.0/account/~/recording/"
        "rec-fixture-001/content"
    ).mock(
        return_value=Response(
            200,
            content=audio,
            headers={"content-type": "audio/mpeg"},
        )
    )
    event = UCWebhookEvent(
        provider="ringcentral",
        external_call_id="s-fixture-rc-001",
        recording_id="rec-fixture-001",
        recording_url=(
            "https://media.ringcentral.com/restapi/v1.0/account/~/"
            "recording/rec-fixture-001/content"
        ),
        raw={"__provider_config": {}},
    )
    fetched = await RingCentralProvider().fetch_recording(
        access_token="fixture-access-token-ringcentral",
        event=event,
    )
    assert fetched.audio_bytes == audio
    assert fetched.content_type == "audio/mpeg"


async def test_ringcentral_duplicate_delivery_does_not_enqueue_twice(
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

    body = json.dumps(
        load_fixture("ringcentral_telephony_session.json")
    ).encode()
    headers = {"verification-token": "rc-fixture-verification-token"}

    r1 = await uc_test_client.post(
        f"/api/v1/uc/ringcentral/webhook/{test_tenant.id}",
        headers=headers,
        content=body,
    )
    assert r1.status_code == 200
    job_id_1 = r1.json()["job_id"]

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(UcRecordingJob).where(
                    UcRecordingJob.provider == "ringcentral"
                )
            )
        ).scalar_one()
        job.state = "in_progress"
        await session.commit()

    enqueued.clear()
    r2 = await uc_test_client.post(
        f"/api/v1/uc/ringcentral/webhook/{test_tenant.id}",
        headers=headers,
        content=body,
    )
    assert r2.status_code == 200
    assert r2.json()["job_id"] == job_id_1
    assert enqueued == []
