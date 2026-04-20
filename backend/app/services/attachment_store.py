"""Attachment object storage — S3 when configured, disabled otherwise.

Keeping this small and pluggable: every path through the code asks the
store "is this available?" before uploading, and gracefully writes an
attachment row with ``s3_key=NULL`` when it isn't.  That keeps dev
loops moving without requiring AWS credentials, at the cost of not
being able to re-attach the file on reply.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, Tuple

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

# S3 layout:
#   s3://{bucket}/tenants/{tenant_id}/attachments/{interaction_id}/{uuid}-{filename}
_KEY_TEMPLATE = "tenants/{tenant_id}/attachments/{interaction_id}/{uuid}-{filename}"

# Cap on what we'll pull into S3 per file — anything larger gets a row
# with size_bytes set but s3_key=NULL. 25 MB matches Gmail's own limit.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AttachmentStore:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    @property
    def available(self) -> bool:
        s = self._settings
        return bool(s.AWS_S3_BUCKET and s.AWS_ACCESS_KEY_ID and s.AWS_SECRET_ACCESS_KEY)

    def _client_lazy(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                aws_access_key_id=self._settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=self._settings.AWS_SECRET_ACCESS_KEY,
                region_name=self._settings.AWS_REGION,
            )
        return self._client

    def put(
        self,
        tenant_id,
        interaction_id,
        filename: str,
        content_type: Optional[str],
        data: bytes,
    ) -> Optional[str]:
        """Upload ``data`` to S3 and return its key.  None if unavailable/too big."""
        if not self.available:
            return None
        if len(data) > MAX_UPLOAD_BYTES:
            logger.info(
                "Attachment %s on interaction %s exceeds %d bytes; storing metadata only",
                filename, interaction_id, MAX_UPLOAD_BYTES,
            )
            return None
        # Keep filename simple/safe — no paths, no control bytes.
        safe_name = "".join(ch for ch in filename if ch.isprintable() and ch not in "/\\") or "file"
        key = _KEY_TEMPLATE.format(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            uuid=uuid.uuid4().hex[:8],
            filename=safe_name,
        )
        try:
            self._client_lazy().put_object(
                Bucket=self._settings.AWS_S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=content_type or "application/octet-stream",
                ServerSideEncryption="AES256",
            )
            return key
        except Exception:
            logger.exception("S3 put failed for attachment %s", filename)
            return None

    def get(self, s3_key: str) -> Optional[Tuple[bytes, Optional[str]]]:
        """Return (bytes, content_type) or None if the object can't be fetched."""
        if not self.available or not s3_key:
            return None
        try:
            resp = self._client_lazy().get_object(
                Bucket=self._settings.AWS_S3_BUCKET, Key=s3_key
            )
            return resp["Body"].read(), resp.get("ContentType")
        except Exception:
            logger.exception("S3 get failed for key %s", s3_key)
            return None

    def presigned_url(self, s3_key: str, expires_in: int = 600) -> Optional[str]:
        if not self.available or not s3_key:
            return None
        try:
            return self._client_lazy().generate_presigned_url(
                "get_object",
                Params={"Bucket": self._settings.AWS_S3_BUCKET, "Key": s3_key},
                ExpiresIn=expires_in,
            )
        except Exception:
            logger.exception("S3 presign failed for key %s", s3_key)
            return None


_store: Optional[AttachmentStore] = None


def get_store() -> AttachmentStore:
    global _store
    if _store is None:
        _store = AttachmentStore()
    return _store
