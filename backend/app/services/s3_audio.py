"""S3 helper for call-recording storage.

Keeps the boto3 dependency scoped to this module so the rest of the app
doesn't import it unless recordings are actually configured. Uses a
tenant-scoped key prefix so objects are trivially easy to restrict with
an S3 bucket policy later.

Envelope envelope:

* ``upload_bytes(tenant_id, recording_id, data, content_type)`` — stream
  raw audio into ``{tenant_id}/{recording_id}.{ext}`` and return the key.
* ``download_and_store_url(tenant_id, recording_id, source_url, *,
  bearer_auth=None)`` — GET the provider's URL (optionally authenticated),
  upload the body to S3, return (s3_key, size_bytes, content_type).
* ``signed_playback_url(s3_key, ttl_seconds=300)`` — short-lived GET URL
  for the UI's ``<audio src=…>`` element.

Everything is synchronous (boto3 is sync-only); callers should push these
into a thread-pool via ``asyncio.to_thread`` when invoked from an async
request handler.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


class S3NotConfigured(RuntimeError):
    """Raised when we try to upload without AWS settings present."""


@dataclass
class StoredAudio:
    s3_key: str
    size_bytes: int
    content_type: str


def _content_type_extension(content_type: str) -> str:
    ct = (content_type or "").lower().split(";")[0].strip()
    guess = mimetypes.guess_extension(ct) if ct else None
    if guess:
        return guess.lstrip(".")
    if ct.endswith("wav"):
        return "wav"
    if ct.endswith("mp3") or ct.endswith("mpeg"):
        return "mp3"
    return "bin"


def _build_key(tenant_id, recording_id, content_type: str) -> str:
    """Deterministic key layout: recordings/{tenant}/{recording}.{ext}.
    Prefix lets us scope bucket IAM policies by tenant later."""
    ext = _content_type_extension(content_type)
    return f"recordings/{tenant_id}/{recording_id}.{ext}"


def _client():
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover — installed in reqs
        raise S3NotConfigured("boto3 is not installed") from exc

    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        raise S3NotConfigured("AWS_S3_BUCKET is not configured")

    kwargs = {"region_name": settings.AWS_REGION or "us-east-1"}
    # Let boto3 pick up credentials from the env / instance profile when
    # explicit keys aren't set. Explicit keys override.
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def upload_bytes(
    *,
    tenant_id,
    recording_id,
    data: bytes,
    content_type: str,
) -> StoredAudio:
    """Synchronous upload — wrap in ``asyncio.to_thread`` when calling from
    async context."""
    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        raise S3NotConfigured("AWS_S3_BUCKET is not configured")

    key = _build_key(tenant_id, recording_id, content_type)
    client = _client()
    client.put_object(
        Bucket=settings.AWS_S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
        # Block public reads — playback goes through signed URLs.
        ACL="private",
    )
    return StoredAudio(
        s3_key=key,
        size_bytes=len(data),
        content_type=content_type or "application/octet-stream",
    )


async def download_and_store_url(
    *,
    tenant_id,
    recording_id,
    source_url: str,
    basic_auth: Optional[Tuple[str, str]] = None,
) -> StoredAudio:
    """Fetch a recording from a provider (Twilio's URL is authenticated
    HTTP Basic; Telnyx pre-signs theirs), then ship it to S3.

    Runs the S3 part through ``asyncio.to_thread`` so the boto3 call
    doesn't block the event loop.
    """
    import asyncio

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(source_url, auth=basic_auth)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Recording fetch failed: {resp.status_code} {resp.text[:300]}"
        )
    content_type = resp.headers.get("Content-Type", "audio/wav")
    data = resp.content
    return await asyncio.to_thread(
        upload_bytes,
        tenant_id=tenant_id,
        recording_id=recording_id,
        data=data,
        content_type=content_type,
    )


def signed_playback_url(s3_key: str, ttl_seconds: int = 300) -> str:
    """Short-lived signed GET URL for browser playback."""
    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        raise S3NotConfigured("AWS_S3_BUCKET is not configured")
    client = _client()
    return client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": settings.AWS_S3_BUCKET, "Key": s3_key},
        ExpiresIn=max(30, min(int(ttl_seconds), 3600)),
    )


def download_to_tempfile(s3_key: str) -> str:
    """Download an S3 object to a NamedTemporaryFile and return its path.

    Used by the transcription worker to stage audio locally before
    handing it to Whisper / pyannote, which both need a file path. The
    caller is responsible for unlinking the file after use.
    """
    import tempfile

    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        raise S3NotConfigured("AWS_S3_BUCKET is not configured")
    ext = s3_key.rsplit(".", 1)[-1] if "." in s3_key else "bin"
    tmp = tempfile.NamedTemporaryFile(prefix="linda-audio-", suffix=f".{ext}", delete=False)
    try:
        client = _client()
        client.download_fileobj(Bucket=settings.AWS_S3_BUCKET, Key=s3_key, Fileobj=tmp)
        tmp.flush()
        return tmp.name
    finally:
        tmp.close()


def delete_object(s3_key: str) -> None:
    """Delete an S3 object. Silently succeeds on 'already gone'."""
    settings = get_settings()
    if not settings.AWS_S3_BUCKET:
        raise S3NotConfigured("AWS_S3_BUCKET is not configured")
    client = _client()
    try:
        client.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=s3_key)
    except Exception as exc:
        if "404" in str(exc) or "NoSuchKey" in str(exc):
            return
        raise


# Re-exported for tests that want to assert on key shape.
__all__ = [
    "StoredAudio",
    "S3NotConfigured",
    "upload_bytes",
    "download_and_store_url",
    "download_to_tempfile",
    "delete_object",
    "signed_playback_url",
    "_build_key",
    "_content_type_extension",
]
