"""End-to-end tests for the Webex Calling UC adapter."""

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

_SECRET = "webex-fixture-webhook-secret"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


async def test_webex_verify_accepts_valid_signature():
    from backend.app.services.telephony.uc.webex import WebexProvider

    body = json.dumps(load_fixture("webex_recording_created.json")).encode()
    provider = WebexProvider()
    event = await provider.verify_webhook(
        headers={"x-spark-signature": _sign(body)},
        body=body,
        signing_secret=_SECRET,
    )
    assert event.provider == "webex_calling"
    assert event.recording_id == "rec-webex-fixture-002"
    assert event.duration_seconds == 312


async def test_webex_verify_rejects_bad_signature():
    from backend.app.services.telephony.uc.base import WebhookVerificationError
    from backend.app.services.telephony.uc.webex import WebexProvider

    body = json.dumps(load_fixture("webex_recording_created.json")).encode()
    provider = WebexProvider()
    with pytest.raises(WebhookVerificationError):
        await provider.verify_webhook(
            headers={"x-spark-signature": _sign(body, "wrong-secret")},
            body=body,
            signing_secret=_SECRET,
        )


async def test_webex_verify_rejects_unsupported_event():
    from backend.app.services.telephony.uc.base import WebhookVerificationError
    from backend.app.services.telephony.uc.webex import WebexProvider

    payload = load_fixture("webex_recording_created.json")
    payload["resource"] = "messages"
    body = json.dumps(payload).encode()
    provider = WebexProvider()
    with pytest.raises(WebhookVerificationError):
        await provider.verify_webhook(
            headers={"x-spark-signature": _sign(body)},
            body=body,
            signing_secret=_SECRET,
        )


@respx.mock
async def test_webex_full_pipeline(
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

    body = json.dumps(load_fixture("webex_recording_created.json")).encode()
    resp = await uc_test_client.post(
        f"/api/v1/uc/webex/webhook/{test_tenant.id}",
        headers={"x-spark-signature": _sign(body)},
        content=body,
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert enqueued == [job_id]

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(UcRecordingJob).where(
                    UcRecordingJob.provider == "webex_calling"
                )
            )
        ).scalar_one()
        assert str(job.id) == job_id
        assert job.recording_id == "rec-webex-fixture-002"

    from backend.app.services.telephony.uc.base import UCWebhookEvent
    from backend.app.services.telephony.uc.webex import WebexProvider

    meta = load_fixture("webex_recording_metadata.json")
    audio = synthetic_mp3()
    respx.get(
        "https://webexapis.com/v1/recordings/rec-webex-fixture-002"
    ).mock(return_value=Response(200, json=meta))
    respx.get(
        meta["temporaryDirectDownloadLinks"]["audioDownloadLink"]
    ).mock(
        return_value=Response(
            200, content=audio, headers={"content-type": "audio/mpeg"}
        )
    )

    event = UCWebhookEvent(
        provider="webex_calling",
        external_call_id="rec-webex-fixture-002",
        recording_id="rec-webex-fixture-002",
        raw={"__provider_config": {}},
    )
    fetched = await WebexProvider().fetch_recording(
        access_token="fixture-access-token-webex_calling",
        event=event,
    )
    assert fetched.audio_bytes == audio
    assert fetched.content_type == "audio/mpeg"


@respx.mock
async def test_webex_fetch_handles_missing_download_link():
    from backend.app.services.telephony.uc.base import (
        UCWebhookEvent,
        WebhookVerificationError,
    )
    from backend.app.services.telephony.uc.webex import WebexProvider

    respx.get(
        "https://webexapis.com/v1/recordings/rec-webex-fixture-002"
    ).mock(return_value=Response(200, json={"id": "rec-webex-fixture-002"}))

    event = UCWebhookEvent(
        provider="webex_calling",
        external_call_id="rec-webex-fixture-002",
        recording_id="rec-webex-fixture-002",
        raw={"__provider_config": {}},
    )
    with pytest.raises(WebhookVerificationError):
        await WebexProvider().fetch_recording(
            access_token="fixture-access-token-webex_calling",
            event=event,
        )
