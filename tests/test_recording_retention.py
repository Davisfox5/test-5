"""Tests for the nightly recording-retention sweep.

Per-tenant ``recording_retention_days`` drives the sweep: rows older
than the cutoff get their S3 object deleted and the row flipped to
``status='deleted'``. Tenants with ``recording_retention_days=0`` are
"keep forever" and skipped entirely.

These tests fake the DB + S3 so no external services are touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List

import pytest

from backend.app.services import recording_retention


def _rec(
    *,
    tenant_id,
    days_old: int,
    status: str = "stored",
    s3_key: str = None,
):
    """Build a fake CallRecording-like row. We use SimpleNamespace so
    mutations (status flip, s3_key clear) survive the test assertion."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        status=status,
        s3_key=s3_key
        or f"recordings/{tenant_id}/{uuid.uuid4()}.wav",
        size_bytes=12345,
        error=None,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDB:
    """Routes queries by their first-FROM entity (Tenant vs
    CallRecording). Good enough for the sweep's two query shapes."""

    def __init__(self, tenants, recordings_by_tenant):
        self.tenants = tenants
        self.recordings_by_tenant = recordings_by_tenant
        self.queries: List[str] = []

    async def execute(self, stmt):
        compiled = str(stmt)
        self.queries.append(compiled)
        if "FROM tenants" in compiled:
            return _FakeResult(self.tenants)
        if "FROM call_recordings" in compiled:
            # Figure out which tenant this query was scoped to by looking
            # for the WHERE tenant_id = <uuid> clause in the recording's
            # captured parameters. Easier: just return every recording
            # that matches the cutoff filter we applied outside.
            tid = None
            # The sweep binds tenant id via WHERE tenant_id = :param_1
            try:
                tid = stmt.compile().params["tenant_id_1"]
            except Exception:
                # Fallback: pick the lone tenant.
                if len(self.recordings_by_tenant) == 1:
                    tid = next(iter(self.recordings_by_tenant.keys()))
            rows = self.recordings_by_tenant.get(tid, [])
            return _FakeResult(rows)
        return _FakeResult([])


@pytest.fixture
def patched_s3(monkeypatch):
    """Capture which S3 keys the sweep asked us to delete so tests can
    assert behaviour without touching boto3."""
    deleted_keys: List[str] = []

    def _fake_delete_object(s3_key: str):
        deleted_keys.append(s3_key)

    monkeypatch.setattr(
        recording_retention, "_delete_object", _fake_delete_object
    )
    return deleted_keys


@pytest.mark.asyncio
async def test_sweep_deletes_expired_recording(patched_s3):
    """A 45-day-old recording under a 30-day policy gets purged."""
    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, recording_retention_days=30)
    rec = _rec(tenant_id=tenant_id, days_old=45)

    db = FakeDB([tenant], {tenant_id: [rec]})
    result = await recording_retention.run_retention_sweep(db)

    assert result.tenants_processed == 1
    assert result.recordings_deleted == 1
    assert rec.status == "deleted"
    assert rec.s3_key is None
    assert rec.size_bytes is None
    # Orig key went to the S3 delete path.
    assert len(patched_s3) == 1


@pytest.mark.asyncio
async def test_sweep_keeps_fresh_recording(patched_s3):
    """A 5-day-old recording under a 30-day policy is kept untouched."""
    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, recording_retention_days=30)
    rec = _rec(tenant_id=tenant_id, days_old=5)

    db = FakeDB([tenant], {tenant_id: []})  # filter excludes fresh rows
    result = await recording_retention.run_retention_sweep(db)

    assert result.recordings_deleted == 0
    # The row itself wasn't touched.
    assert rec.status == "stored"
    assert rec.s3_key is not None
    # And S3 was never called.
    assert patched_s3 == []


@pytest.mark.asyncio
async def test_sweep_skips_tenants_with_zero_retention(patched_s3):
    """``recording_retention_days=0`` is the keep-forever signal. The
    SQL query filters these out, so the sweep never even enumerates
    recordings for them."""
    tenant_id = uuid.uuid4()
    # retention=0 tenant should not even appear in the tenants result.
    db = FakeDB([], {tenant_id: []})
    result = await recording_retention.run_retention_sweep(db)
    assert result.tenants_processed == 0
    assert result.recordings_deleted == 0
    assert patched_s3 == []


@pytest.mark.asyncio
async def test_sweep_handles_s3_not_configured(monkeypatch):
    """When S3 isn't set up, rows still flip to ``deleted`` so we don't
    re-process them every night — we just don't delete bytes."""
    from backend.app.services import s3_audio

    def _raises_not_configured(s3_key: str):
        raise s3_audio.S3NotConfigured("AWS_S3_BUCKET is not configured")

    monkeypatch.setattr(
        recording_retention, "_delete_object", _raises_not_configured
    )

    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, recording_retention_days=30)
    rec = _rec(tenant_id=tenant_id, days_old=60)

    db = FakeDB([tenant], {tenant_id: [rec]})
    result = await recording_retention.run_retention_sweep(db)

    # Row is still marked deleted (we don't want to keep finding it
    # every sweep), but we don't count it as an S3 error.
    assert rec.status == "deleted"
    assert result.recordings_deleted == 1
    assert result.s3_errors == 0


@pytest.mark.asyncio
async def test_sweep_treats_404_as_success(monkeypatch):
    """If the S3 object is already gone, that's the desired end state —
    we treat it as a successful delete."""
    def _raises_404(s3_key: str):
        raise Exception("An error occurred (404) when calling DeleteObject: NoSuchKey")

    monkeypatch.setattr(
        recording_retention, "_delete_object", _raises_404
    )

    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, recording_retention_days=30)
    rec = _rec(tenant_id=tenant_id, days_old=60)

    db = FakeDB([tenant], {tenant_id: [rec]})
    result = await recording_retention.run_retention_sweep(db)

    assert rec.status == "deleted"
    assert rec.s3_key is None
    assert result.recordings_deleted == 1
    assert result.s3_errors == 0


@pytest.mark.asyncio
async def test_sweep_records_s3_errors(monkeypatch):
    """Genuine S3 failures (permissions, 500s) get counted, and the
    affected row keeps its ``status='stored'`` so the next sweep can
    retry."""
    def _raises_500(s3_key: str):
        raise Exception("An error occurred (500) InternalError")

    monkeypatch.setattr(
        recording_retention, "_delete_object", _raises_500
    )

    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, recording_retention_days=30)
    rec = _rec(tenant_id=tenant_id, days_old=60)

    db = FakeDB([tenant], {tenant_id: [rec]})
    result = await recording_retention.run_retention_sweep(db)

    # Row stays ``stored`` so the next sweep retries; error stored on row.
    assert rec.status == "stored"
    assert rec.error is not None
    assert result.s3_errors == 1
    assert result.recordings_deleted == 0


@pytest.mark.asyncio
async def test_sweep_handles_row_with_no_s3_key(patched_s3):
    """A recording row whose ``s3_key`` is empty (e.g. failed upload)
    should still flip to ``deleted`` so we stop picking it up, but we
    shouldn't call S3."""
    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(id=tenant_id, recording_retention_days=30)
    rec = _rec(tenant_id=tenant_id, days_old=60, s3_key="")
    rec.s3_key = None

    db = FakeDB([tenant], {tenant_id: [rec]})
    result = await recording_retention.run_retention_sweep(db)

    assert rec.status == "deleted"
    assert result.recordings_deleted == 1
    # No S3 call since there was no key to delete.
    assert patched_s3 == []
