"""Tests for the voice-upload pipeline.

``POST /interactions/upload`` does three things in order:

1. Validates content type + size, creates an ``Interaction`` row.
2. Streams the bytes into S3 under ``uploads/{tenant}/{id}.{ext}`` and
   pins ``audio_s3_key`` on the row.
3. Dispatches the Celery task that actually transcribes + analyses.

These tests cover each branch — success, missing S3 config, and the
Celery-unavailable fallback — by calling the endpoint function
directly with fakes for the DB, S3, and Celery.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import List

import pytest

from backend.app.api import interactions as interactions_module
from backend.app.services import s3_audio


class _FakeUploadFile:
    """Minimal stand-in for ``fastapi.UploadFile`` — only needs the
    attributes the endpoint actually reads."""

    def __init__(self, content: bytes, filename="call.wav", content_type="audio/wav"):
        self._content = content
        self.filename = filename
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._content


class FakeDB:
    """Collect the rows the endpoint tries to flush so tests can
    inspect them."""

    def __init__(self):
        self.added: list = []
        self.flushes = 0

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushes += 1


@pytest.fixture
def fake_tenant():
    return SimpleNamespace(id=uuid.uuid4())


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.mark.asyncio
async def test_upload_happy_path(monkeypatch, fake_tenant, fake_db):
    """Happy path: file is uploaded to S3, the row stores the S3 key,
    and Celery's ``delay`` is called with the interaction id."""
    stored = s3_audio.StoredAudio(
        s3_key="recordings/t/r.wav",
        size_bytes=12,
        content_type="audio/wav",
    )

    def _fake_upload_bytes(*, tenant_id, recording_id, data, content_type):
        return stored

    monkeypatch.setattr(s3_audio, "upload_bytes", _fake_upload_bytes)

    dispatched: List[str] = []

    def _fake_delay(interaction_id):
        dispatched.append(interaction_id)

    fake_task = SimpleNamespace(delay=_fake_delay)
    import backend.app.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "process_voice_interaction", fake_task)

    upload = _FakeUploadFile(b"raw-wav-bytes")
    result = await interactions_module.upload_voice_interaction(
        file=upload,
        title="lead call",
        engine="deepgram",
        caller_phone="+15551234",
        agent_id=None,
        db=fake_db,
        tenant=fake_tenant,
    )

    # Row flushed once, S3 key pinned, Celery dispatched.
    assert fake_db.flushes == 1
    assert result.audio_s3_key == "recordings/t/r.wav"
    assert result.status == "processing"
    assert dispatched == [str(result.id)]


@pytest.mark.asyncio
async def test_upload_fails_when_s3_not_configured(monkeypatch, fake_tenant, fake_db):
    """Without S3 the pipeline cannot run, so the row is marked
    ``failed`` and the caller sees the configuration error in
    ``insights.error`` rather than a silent never-processed row."""
    def _raises(**_):
        raise s3_audio.S3NotConfigured("AWS_S3_BUCKET is not configured")

    monkeypatch.setattr(s3_audio, "upload_bytes", _raises)

    # Celery should never be called on this branch.
    dispatched: List[str] = []
    import backend.app.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "process_voice_interaction",
        SimpleNamespace(delay=lambda _id: dispatched.append(_id)),
    )

    upload = _FakeUploadFile(b"raw-wav-bytes")
    result = await interactions_module.upload_voice_interaction(
        file=upload,
        title=None,
        engine="deepgram",
        caller_phone=None,
        agent_id=None,
        db=fake_db,
        tenant=fake_tenant,
    )

    assert result.status == "failed"
    assert result.insights == {"error": "audio_storage_not_configured"}
    assert result.audio_s3_key is None
    assert dispatched == []  # no Celery dispatch on failed upload


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_content_type(fake_tenant, fake_db):
    """The endpoint gates on content-type before reading bytes."""
    from fastapi import HTTPException

    upload = _FakeUploadFile(b"x", content_type="application/pdf")
    with pytest.raises(HTTPException) as exc_info:
        await interactions_module.upload_voice_interaction(
            file=upload,
            title=None,
            engine="deepgram",
            caller_phone=None,
            agent_id=None,
            db=fake_db,
            tenant=fake_tenant,
        )
    assert exc_info.value.status_code == 400
    assert "Unsupported file type" in exc_info.value.detail


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(fake_tenant, fake_db):
    """500MB hard limit — avoid loading a gigabyte of audio into memory
    and punishing the request path."""
    from fastapi import HTTPException

    upload = _FakeUploadFile(b"x" * (501 * 1024 * 1024))
    with pytest.raises(HTTPException) as exc_info:
        await interactions_module.upload_voice_interaction(
            file=upload,
            title=None,
            engine="deepgram",
            caller_phone=None,
            agent_id=None,
            db=fake_db,
            tenant=fake_tenant,
        )
    assert exc_info.value.status_code == 400
    assert "too large" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_upload_survives_celery_unavailable(monkeypatch, fake_tenant, fake_db):
    """Celery being offline (common in dev / tests) should NOT fail the
    upload — the row is saved, admins can run analysis later."""
    stored = s3_audio.StoredAudio(
        s3_key="recordings/t/r.wav", size_bytes=5, content_type="audio/wav"
    )
    monkeypatch.setattr(s3_audio, "upload_bytes", lambda **_: stored)

    def _celery_down(*_args, **_kwargs):
        raise RuntimeError("celery broker unreachable")

    import backend.app.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "process_voice_interaction",
        SimpleNamespace(delay=_celery_down),
    )

    upload = _FakeUploadFile(b"audio")
    result = await interactions_module.upload_voice_interaction(
        file=upload,
        title=None,
        engine="deepgram",
        caller_phone=None,
        agent_id=None,
        db=fake_db,
        tenant=fake_tenant,
    )

    # Row still saved with S3 key — the admin can retrigger analysis.
    assert result.status == "processing"
    assert result.audio_s3_key == "recordings/t/r.wav"


@pytest.mark.asyncio
async def test_upload_marks_failed_on_unexpected_s3_error(monkeypatch, fake_tenant, fake_db):
    """A non-S3NotConfigured exception from boto3 (throttling, 500) is
    still caught — the row is marked failed with the error message
    so the admin can see what went wrong."""
    def _boom(**_):
        raise RuntimeError("throttled by S3")

    monkeypatch.setattr(s3_audio, "upload_bytes", _boom)

    dispatched: List[str] = []
    import backend.app.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "process_voice_interaction",
        SimpleNamespace(delay=lambda _id: dispatched.append(_id)),
    )

    upload = _FakeUploadFile(b"bytes")
    result = await interactions_module.upload_voice_interaction(
        file=upload,
        title=None,
        engine="deepgram",
        caller_phone=None,
        agent_id=None,
        db=fake_db,
        tenant=fake_tenant,
    )

    assert result.status == "failed"
    assert "upload_failed" in (result.insights or {}).get("error", "")
    assert dispatched == []
