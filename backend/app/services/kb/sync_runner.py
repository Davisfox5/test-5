"""Run one KB provider sync for a tenant.

Resolves the provider's OAuth credentials from the Integration row,
instantiates the right adapter, iterates documents, and upserts each
through :func:`backend.app.services.kb.ingest.ingest_document` (which
handles chunking, embedding, and vector-store writes).

Upsert key is ``(tenant_id, source_type, source_external_id)`` so a
re-run updates existing docs instead of duplicating. Content-hash
checking inside ``ingest_document`` skips unchanged docs without
re-embedding.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Integration, KBDocument
from backend.app.services.kb.ingest import ingest_document
from backend.app.services.kb.providers import (
    ExternalDocument,
    KBProvider,
    KBProviderAuthError,
    KBProviderError,
)
from backend.app.services.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


SUPPORTED_PROVIDERS = {"gdrive", "onedrive", "sharepoint", "confluence"}


@dataclass
class KBSyncSummary:
    source_type: str
    status: str  # success | partial | failed
    docs_seen: int
    docs_upserted: int
    chunks_written: int
    error: Optional[str] = None


async def sync_kb_for_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    source_type: str,
) -> KBSyncSummary:
    if source_type not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported KB source_type: {source_type}")

    try:
        provider = await _build_provider(db, tenant_id, source_type)
    except KBProviderAuthError as exc:
        return KBSyncSummary(source_type, "failed", 0, 0, 0, error=str(exc))

    docs_seen = 0
    docs_upserted = 0
    chunks_written = 0
    partial = False

    try:
        async for external in provider.iter_documents():
            docs_seen += 1
            try:
                chunks = await _upsert_external_document(db, tenant_id, external)
                docs_upserted += 1
                chunks_written += chunks
            except Exception:
                logger.exception(
                    "KB ingest failed for %s:%s",
                    external.source_type,
                    external.external_id,
                )
                partial = True
    except KBProviderAuthError as exc:
        return KBSyncSummary(
            source_type, "failed", docs_seen, docs_upserted, chunks_written, error=str(exc)
        )
    except KBProviderError as exc:
        return KBSyncSummary(
            source_type, "partial" if docs_upserted else "failed",
            docs_seen, docs_upserted, chunks_written, error=str(exc),
        )
    finally:
        try:
            await provider.close()
        except Exception:
            logger.debug("provider.close failed", exc_info=True)

    status = "partial" if partial else "success"
    return KBSyncSummary(source_type, status, docs_seen, docs_upserted, chunks_written)


async def _upsert_external_document(
    db: AsyncSession, tenant_id: uuid.UUID, external: ExternalDocument
) -> int:
    """Find an existing KBDocument by ``(tenant, source_type, source_external_id)``
    and update it; otherwise insert. Returns chunks written.
    """
    stmt = select(KBDocument).where(
        KBDocument.tenant_id == tenant_id,
        KBDocument.source_type == external.source_type,
        KBDocument.source_external_id == external.external_id,
    )
    doc = (await db.execute(stmt)).scalar_one_or_none()
    if doc is None:
        doc = KBDocument(
            tenant_id=tenant_id,
            title=external.title,
            content=external.content,
            source_type=external.source_type,
            source_url=external.source_url,
            source_external_id=external.external_id,
            tags=list(external.tags or []),
        )
        db.add(doc)
        await db.flush()
    else:
        doc.title = external.title or doc.title
        doc.content = external.content
        doc.source_url = external.source_url or doc.source_url
        if external.tags:
            doc.tags = list(external.tags)
        doc.last_synced_at = datetime.now(timezone.utc)

    return await ingest_document(db, doc)


async def _build_provider(
    db: AsyncSession, tenant_id: uuid.UUID, source_type: str
) -> KBProvider:
    """Load the Integration row and instantiate the right provider."""
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == source_type,
        )
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        raise KBProviderAuthError(
            f"No {source_type} integration for tenant {tenant_id}"
        )

    access_token = decrypt_token(integ.access_token) or ""
    refresh_token = decrypt_token(integ.refresh_token)
    cfg = integ.provider_config or {}

    async def on_refresh(
        new_access: str, new_refresh: Optional[str], expires_in: Optional[int]
    ) -> None:
        integ.access_token = encrypt_token(new_access)
        if new_refresh:
            integ.refresh_token = encrypt_token(new_refresh)
        if expires_in:
            from datetime import timedelta

            integ.expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )

    if source_type == "gdrive":
        from backend.app.services.kb.providers.gdrive import GoogleDriveProvider

        return GoogleDriveProvider(
            access_token=access_token,
            refresh_token=refresh_token,
            folder_id=cfg.get("folder_id"),
            drive_id=cfg.get("drive_id"),
            on_token_refresh=on_refresh,
        )
    if source_type in ("onedrive", "sharepoint"):
        from backend.app.services.kb.providers.onedrive import (
            OneDriveProvider,
            SharePointProvider,
        )

        klass = SharePointProvider if source_type == "sharepoint" else OneDriveProvider
        return klass(
            access_token=access_token,
            refresh_token=refresh_token,
            drive_id=cfg.get("drive_id"),
            folder_path=cfg.get("folder_path"),
            tenant_identifier=cfg.get("ms_tenant_id") or "common",
            scopes=cfg.get("scopes"),
            on_token_refresh=on_refresh,
        )
    if source_type == "confluence":
        from backend.app.services.kb.providers.confluence import ConfluenceProvider

        return ConfluenceProvider(
            base_url=cfg.get("base_url") or "",
            auth_mode=cfg.get("auth_mode") or "basic",
            email=cfg.get("email"),
            api_token=access_token,  # for Cloud: api_token sits in access_token
            personal_access_token=access_token if cfg.get("auth_mode") == "bearer" else None,
            space_keys=cfg.get("space_keys") or [],
        )
    raise ValueError(f"Unhandled KB provider: {source_type}")
