"""Celery task: fetch a UC vendor recording, stage it in S3, dispatch
the existing voice pipeline.

Dispatched by :mod:`backend.app.api.uc_telephony` after a webhook
delivery has been authenticated and a :class:`UcRecordingJob` row has
been written. The job row is the idempotency anchor — late duplicates
become no-ops because we update an existing row to ``in_progress``.

State machine (column ``state``):

    pending → in_progress → fetched → dispatched → done
                  ↓
                failed (with attempts++ and error string)

We don't retry failed jobs automatically — a failed fetch usually
means the OAuth token was revoked or the recording was deleted, and
the right correction is operator-visible (admin reconnects integration,
re-runs job from /admin) rather than blind retries.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from backend.app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="fetch_uc_recording",
    max_retries=2,
    default_retry_delay=60,
)
def fetch_uc_recording(self, job_id: str) -> Dict[str, Any]:
    """Fetch + dispatch one UC recording job.

    ``job_id`` is the UUID string of a :class:`UcRecordingJob` row.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from backend.app.models import Integration, Interaction, UcRecordingJob
    from backend.app.services import s3_audio
    from backend.app.services.telephony.uc.base import (
        UCWebhookEvent,
        WebhookVerificationError,
        get_provider,
    )
    from backend.app.services.token_crypto import decrypt_token
    from backend.app.tasks import _get_sync_session, process_voice_interaction

    job_uuid = uuid.UUID(job_id)
    session: Session = _get_sync_session()

    try:
        job = session.execute(
            select(UcRecordingJob).where(UcRecordingJob.id == job_uuid)
        ).scalar_one_or_none()
        if job is None:
            logger.error("UcRecordingJob %s not found", job_id)
            return {"status": "missing", "job_id": job_id}

        if job.state in ("done", "dispatched"):
            return {
                "status": "already_done",
                "job_id": job_id,
                "interaction_id": str(job.interaction_id) if job.interaction_id else None,
            }

        job.state = "in_progress"
        job.attempts = (job.attempts or 0) + 1
        session.commit()

        integration = session.get(Integration, job.integration_id)
        if integration is None:
            _mark_failed(session, job, "integration row missing")
            return {"status": "failed", "job_id": job_id, "error": "integration missing"}

        access_token = decrypt_token(integration.access_token) or ""
        if not access_token:
            _mark_failed(session, job, "integration has no decrypted access token")
            return {"status": "failed", "job_id": job_id, "error": "no access token"}

        provider = get_provider(job.provider)
        event = UCWebhookEvent(
            provider=job.provider,
            external_call_id=job.external_call_id,
            recording_id=job.recording_id,
            recording_url=job.recording_url,
            duration_seconds=job.duration_seconds,
            started_at=job.started_at_provider,
            direction=job.direction,
            caller_phone=job.caller_phone,
            callee_phone=job.callee_phone,
            raw={
                **(job.payload or {}),
                "__provider_config": integration.provider_config or {},
            },
        )

        try:
            fetched = asyncio.run(
                provider.fetch_recording(access_token=access_token, event=event)
            )
        except WebhookVerificationError as exc:
            _mark_failed(session, job, f"fetch failed: {exc}")
            return {"status": "failed", "job_id": job_id, "error": str(exc)}
        except Exception as exc:
            _mark_failed(session, job, f"fetch errored: {exc}")
            try:
                raise self.retry(exc=exc, countdown=60)
            except Exception:
                return {"status": "failed", "job_id": job_id, "error": str(exc)}

        job.state = "fetched"
        session.commit()

        try:
            interaction = Interaction(
                tenant_id=job.tenant_id,
                channel="voice",
                source=job.provider,
                direction=event.direction,
                title=f"{_human_provider(job.provider)} recording {event.recording_id}",
                caller_phone=event.caller_phone,
                engine="deepgram",
                status="processing",
                duration_seconds=event.duration_seconds,
                thread_id=event.external_call_id,
            )
            session.add(interaction)
            session.flush()

            stored = s3_audio.upload_bytes(
                tenant_id=job.tenant_id,
                recording_id=interaction.id,
                data=fetched.audio_bytes,
                content_type=fetched.content_type,
            )
            interaction.audio_s3_key = stored.s3_key

            job.interaction_id = interaction.id
            job.state = "dispatched"
            session.commit()
        except Exception as exc:
            _mark_failed(session, job, f"stage failed: {exc}")
            session.rollback()
            try:
                raise self.retry(exc=exc, countdown=60)
            except Exception:
                return {"status": "failed", "job_id": job_id, "error": str(exc)}

        try:
            process_voice_interaction.delay(str(interaction.id))
        except Exception:
            logger.exception(
                "Celery dispatch of process_voice_interaction failed for %s",
                interaction.id,
            )

        job.state = "done"
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        return {
            "status": "done",
            "job_id": job_id,
            "interaction_id": str(interaction.id),
        }
    finally:
        session.close()


def _mark_failed(session, job, error: str) -> None:
    job.state = "failed"
    job.last_error = error[:500]
    job.finished_at = datetime.now(timezone.utc)
    session.commit()


def _human_provider(name: str) -> str:
    return {
        "ringcentral": "RingCentral",
        "webex_calling": "Webex",
        "zoom_phone": "Zoom Phone",
    }.get(name, name)


__all__ = ["fetch_uc_recording"]
