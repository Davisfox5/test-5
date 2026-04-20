"""Call-recording retention sweeper.

Runs daily (scheduled by Celery beat). For each tenant with a non-zero
``recording_retention_days`` setting, finds ``CallRecording`` rows
older than the cutoff and:

1. Deletes the S3 object under ``recordings/{tenant_id}/{recording_id}.{ext}``.
2. Flips the row's ``status`` to ``"deleted"`` and clears ``s3_key`` +
   ``size_bytes``. We keep the audit row (with timestamps + duration)
   so compliance can prove the recording *existed* even after the bytes
   are gone — just that we no longer have the audio.

Idempotent: a row that's already ``status="deleted"`` is skipped. S3
"object not found" during delete is treated as success (it's the state
we want).

Tenants with ``recording_retention_days=0`` are skipped entirely —
that's the "keep forever" signal.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import CallRecording, Tenant

logger = logging.getLogger(__name__)


@dataclass
class RetentionSweepResult:
    tenants_processed: int = 0
    recordings_deleted: int = 0
    s3_errors: int = 0
    per_tenant: List[Dict] = field(default_factory=list)


async def run_retention_sweep(
    db: AsyncSession, *, now: Optional[datetime] = None
) -> RetentionSweepResult:
    """Walk every tenant with retention configured + purge expired recordings."""
    result = RetentionSweepResult()
    current = now or datetime.now(timezone.utc)

    tenants = list(
        (
            await db.execute(
                select(Tenant).where(Tenant.recording_retention_days > 0)
            )
        )
        .scalars()
        .all()
    )

    for tenant in tenants:
        summary = await _sweep_tenant(db, tenant, current)
        result.tenants_processed += 1
        result.recordings_deleted += summary["deleted"]
        result.s3_errors += summary["s3_errors"]
        result.per_tenant.append(summary)

    return result


async def _sweep_tenant(
    db: AsyncSession, tenant: Tenant, current: datetime
) -> Dict:
    """Delete recordings older than ``tenant.recording_retention_days``."""
    cutoff = current - timedelta(days=int(tenant.recording_retention_days))
    stmt = (
        select(CallRecording)
        .where(
            CallRecording.tenant_id == tenant.id,
            CallRecording.status == "stored",
            CallRecording.created_at < cutoff,
        )
    )
    rows = list((await db.execute(stmt)).scalars().all())

    deleted = 0
    s3_errors = 0

    # Import lazily so tests that don't touch S3 don't need boto3.
    from backend.app.services import s3_audio

    for rec in rows:
        if not rec.s3_key:
            # Nothing to delete — mark the row so we don't keep picking
            # it up on every sweep.
            rec.status = "deleted"
            deleted += 1
            continue

        try:
            await asyncio.to_thread(_delete_object, rec.s3_key)
        except s3_audio.S3NotConfigured:
            # Retention configured but S3 isn't — log and move on; we
            # still mark rows ``deleted`` so the row count stays truthful.
            logger.warning(
                "Retention: S3 not configured for tenant %s; marking recording "
                "%s as deleted without deleting bytes.",
                tenant.id,
                rec.id,
            )
        except Exception as exc:
            # A 404 is a happy path (we wanted the object gone). Boto3
            # surfaces them via the ClientError type; avoid importing it
            # eagerly — we inspect the string representation.
            if "404" in str(exc) or "NoSuchKey" in str(exc):
                pass
            else:
                logger.exception(
                    "Retention: S3 delete failed for recording %s", rec.id
                )
                rec.error = str(exc)[:500]
                s3_errors += 1
                continue

        rec.status = "deleted"
        rec.s3_key = None
        rec.size_bytes = None
        deleted += 1

    return {
        "tenant_id": str(tenant.id),
        "retention_days": int(tenant.recording_retention_days),
        "deleted": deleted,
        "s3_errors": s3_errors,
    }


def _delete_object(s3_key: str) -> None:
    """Synchronous S3 delete. Called from a thread pool so the async
    loop stays unblocked."""
    from backend.app.config import get_settings
    from backend.app.services import s3_audio

    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        raise s3_audio.S3NotConfigured("AWS_S3_BUCKET is not configured")
    client = s3_audio._client()
    client.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=s3_key)
