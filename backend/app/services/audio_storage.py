"""Audio storage — put/get/delete with tenant-configurable retention.

Goals per ``docs/SCORING_ARCHITECTURE.md``:

- Upload audio to S3 on ingest (or after live capture) so the pipeline
  can transcribe and extract paralinguistic features.
- Tag each object with ``tenant_id``, ``retention_hours``, and
  ``stored_at`` so a nightly janitor (or S3 lifecycle rule) can expire
  objects without touching the hot pipeline.
- Expire audio by ``Tenant.audio_retention_hours`` (default 24h).
  Tenants that opt into longer retention (replay, re-transcription)
  override the default; the janitor still enforces the cap.
- Never fail the pipeline if S3 is unavailable — the paralinguistic
  extractor degrades gracefully, and the deterministic feature
  extractor runs entirely off the transcript.

The module is careful to work without ``boto3`` installed.  In that
case the :class:`AudioStore` falls back to a local-filesystem
implementation at ``$AUDIO_LOCAL_DIR`` (default ``/tmp/callsight-audio``)
so developers can run the pipeline end-to-end without cloud creds.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Environment knobs ────────────────────────────────────────────────────

BUCKET_ENV = "AUDIO_BUCKET"
LOCAL_DIR_ENV = "AUDIO_LOCAL_DIR"
DEFAULT_LOCAL_DIR = "/tmp/callsight-audio"


# ── Contract ─────────────────────────────────────────────────────────────


@dataclass
class AudioHandle:
    """Pointer to an uploaded audio artifact.

    ``s3_key`` is always populated.  ``local_path`` is populated when
    the local-fs fallback is in use so the paralinguistic extractor can
    consume the file directly.
    """

    s3_key: str
    tenant_id: str
    stored_at: datetime
    retention_hours: int
    backend: str  # "s3" | "local" | "none"
    local_path: Optional[str] = None


# ── Core store ───────────────────────────────────────────────────────────


class AudioStore:
    """S3-backed audio store with a local-filesystem fallback.

    Calling pattern
    ---------------
    ``handle = store.put(tenant_id=..., interaction_id=..., audio_bytes=...,
                          retention_hours=24)``
    — upload + return a handle suitable for paralinguistic extraction.

    ``store.get_local_path(handle)`` — local filesystem path callers can
    hand to praat-parselmouth.  Materializes the object if needed.

    ``store.delete(handle)`` — drop the object (called by the janitor
    after the retention window closes).
    """

    def __init__(self) -> None:
        self._bucket: Optional[str] = os.environ.get(BUCKET_ENV) or None
        self._local_dir = Path(os.environ.get(LOCAL_DIR_ENV, DEFAULT_LOCAL_DIR))
        self._local_dir.mkdir(parents=True, exist_ok=True)
        self._s3: Any = None
        if self._bucket:
            try:
                import boto3  # type: ignore
                self._s3 = boto3.client("s3")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AudioStore: bucket configured but boto3 unavailable (%s); "
                    "falling back to local filesystem", exc,
                )
                self._bucket = None

    @property
    def backend(self) -> str:
        if self._bucket and self._s3 is not None:
            return "s3"
        return "local"

    # ── put / get / delete ───────────────────────────────────────────

    def put(
        self,
        *,
        tenant_id: str,
        interaction_id: str,
        audio_bytes: bytes,
        retention_hours: int,
        content_type: str = "audio/mpeg",
    ) -> AudioHandle:
        key = self._object_key(tenant_id, interaction_id)
        stored_at = datetime.now(timezone.utc)

        if self.backend == "s3":
            try:
                self._s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=audio_bytes,
                    ContentType=content_type,
                    Tagging=self._build_tagging(tenant_id, retention_hours, stored_at),
                )
                return AudioHandle(
                    s3_key=key,
                    tenant_id=tenant_id,
                    stored_at=stored_at,
                    retention_hours=retention_hours,
                    backend="s3",
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "AudioStore S3 put failed for %s; falling back to local",
                    interaction_id,
                )

        local_path = self._local_dir / self._safe_filename(key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(audio_bytes)
        # Sidecar metadata so the janitor can compute expiry without
        # relying on filesystem mtime.
        (local_path.with_suffix(local_path.suffix + ".meta")).write_text(
            f"tenant_id={tenant_id}\n"
            f"retention_hours={retention_hours}\n"
            f"stored_at={stored_at.isoformat()}\n"
        )
        return AudioHandle(
            s3_key=key,
            tenant_id=tenant_id,
            stored_at=stored_at,
            retention_hours=retention_hours,
            backend="local",
            local_path=str(local_path),
        )

    def get_local_path(self, handle: AudioHandle) -> Optional[str]:
        """Materialize the audio to a local path the extractor can read.

        For the local backend this is a no-op; for S3 we download to a
        scratch file that the caller is responsible for deleting (or
        letting the OS clean up via ``/tmp``).
        """
        if handle.backend == "local" and handle.local_path:
            return handle.local_path
        if self.backend == "s3" and self._s3 is not None:
            dest = self._local_dir / self._safe_filename(handle.s3_key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._s3.download_file(self._bucket, handle.s3_key, str(dest))
                return str(dest)
            except Exception:  # noqa: BLE001
                logger.exception("AudioStore S3 download failed for %s", handle.s3_key)
                return None
        return None

    def delete(self, handle: AudioHandle) -> bool:
        """Drop the object.  Returns True on success or when the object was
        already absent; False only on an unexpected backend error.
        """
        if handle.backend == "s3" and self._s3 is not None:
            try:
                self._s3.delete_object(Bucket=self._bucket, Key=handle.s3_key)
                return True
            except Exception:  # noqa: BLE001
                logger.exception("AudioStore S3 delete failed for %s", handle.s3_key)
                return False
        # Local backend — best-effort unlink of both the object and its sidecar.
        if handle.local_path:
            p = Path(handle.local_path)
            try:
                if p.exists():
                    p.unlink()
                sidecar = p.with_suffix(p.suffix + ".meta")
                if sidecar.exists():
                    sidecar.unlink()
                return True
            except OSError:
                logger.exception("AudioStore local delete failed for %s", p)
                return False
        return True

    # ── Janitor ──────────────────────────────────────────────────────

    def sweep_expired(self) -> int:
        """Delete every object past its retention window.  Returns count."""
        now = datetime.now(timezone.utc)
        deleted = 0
        if self.backend == "s3" and self._s3 is not None:
            # Rely on the tags we set at put-time; pagination handled by boto3.
            paginator = self._s3.get_paginator("list_objects_v2")
            try:
                for page in paginator.paginate(Bucket=self._bucket):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        try:
                            tags = {
                                t["Key"]: t["Value"]
                                for t in self._s3.get_object_tagging(
                                    Bucket=self._bucket, Key=key
                                )["TagSet"]
                            }
                        except Exception:
                            continue
                        if self._expired(tags, now):
                            try:
                                self._s3.delete_object(Bucket=self._bucket, Key=key)
                                deleted += 1
                            except Exception:
                                logger.exception("Sweep delete failed for %s", key)
            except Exception:
                logger.exception("Sweep list failed")
            return deleted

        # Local filesystem.
        for meta in self._local_dir.rglob("*.meta"):
            try:
                tags = dict(
                    line.strip().split("=", 1)
                    for line in meta.read_text().splitlines()
                    if "=" in line
                )
            except Exception:
                continue
            if self._expired(tags, now):
                audio = meta.with_suffix("")
                try:
                    if audio.exists():
                        audio.unlink()
                    meta.unlink()
                    deleted += 1
                except OSError:
                    logger.exception("Local sweep failed for %s", audio)
        return deleted

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _object_key(tenant_id: str, interaction_id: str) -> str:
        return f"{tenant_id}/{interaction_id}.audio"

    @staticmethod
    def _build_tagging(tenant_id: str, retention_hours: int, stored_at: datetime) -> str:
        # Tags are URL-encoded key=value&key=value.
        parts = [
            f"tenant_id={tenant_id}",
            f"retention_hours={retention_hours}",
            f"stored_at={stored_at.isoformat()}",
        ]
        return "&".join(parts)

    @staticmethod
    def _safe_filename(key: str) -> str:
        # Replace path separators with underscores so the local backend
        # stays flat.
        return key.replace("/", "_")

    @staticmethod
    def _expired(tags: dict, now: datetime) -> bool:
        try:
            retention = int(tags.get("retention_hours", 24))
            stored_at = datetime.fromisoformat(tags["stored_at"])
            return now - stored_at > timedelta(hours=retention)
        except (KeyError, ValueError, TypeError):
            return False


_default_store: Optional[AudioStore] = None


def get_audio_store() -> AudioStore:
    global _default_store
    if _default_store is None:
        _default_store = AudioStore()
    return _default_store
