"""Pull KB documents from a Model Context Protocol (MCP) server.

The tenant registers one or more MCP servers as
``Integration(provider='mcp', provider_config={name, endpoint, secret,
list_tool, get_tool})``. When a sync is triggered we:

1. POST to ``{endpoint}/{list_tool}`` with the shared bearer secret.
   The server returns a list of ``{external_id, title, source_url,
   updated_at, tags}`` shapes.
2. POST to ``{endpoint}/{get_tool}`` for each id to fetch ``content``.
3. Upsert each document via the existing KB ingest pipeline.

This gives customers a one-file contract for plugging any internal
knowledge system into LINDA without us maintaining another adapter.
MCP semantics are a good fit: discover tools via a list, invoke them,
stream results.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Integration, KBDocument
from backend.app.services.kb.ingest import ingest_document
from backend.app.services.token_crypto import decrypt_token

logger = logging.getLogger(__name__)


async def pull_from_mcp(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    server_name: str,
) -> Dict[str, Any]:
    """Pull every document from the named MCP server and ingest it.

    Returns a summary dict ``{server, docs_seen, docs_upserted,
    chunks_written, errors}``.
    """
    integ = await _load_mcp_integration(db, tenant_id, server_name)
    cfg = integ.provider_config or {}
    endpoint = str(cfg.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise RuntimeError("MCP endpoint missing from provider_config")
    secret = decrypt_token(integ.access_token) or ""
    list_tool = str(cfg.get("list_tool") or "kb/list")
    get_tool = str(cfg.get("get_tool") or "kb/get")

    docs_seen = 0
    docs_upserted = 0
    chunks_written = 0
    errors: List[str] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        cursor: Optional[str] = None
        while True:
            payload = {"cursor": cursor} if cursor else {}
            resp = await client.post(
                f"{endpoint}/{list_tool}", json=payload, headers=headers
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"MCP list failed: {resp.status_code} {resp.text[:200]}"
                )
            listing = resp.json()
            entries = listing.get("documents") or []
            for entry in entries:
                docs_seen += 1
                ext_id = str(entry.get("external_id") or entry.get("id") or "")
                if not ext_id:
                    errors.append("missing external_id in listing")
                    continue
                try:
                    detail_resp = await client.post(
                        f"{endpoint}/{get_tool}",
                        json={"id": ext_id},
                        headers=headers,
                    )
                    if detail_resp.status_code >= 400:
                        errors.append(
                            f"get {ext_id}: {detail_resp.status_code}"
                        )
                        continue
                    detail = detail_resp.json()
                    content = str(detail.get("content") or "").strip()
                    if not content:
                        continue
                    chunks = await _upsert_mcp_doc(
                        db,
                        tenant_id=tenant_id,
                        server_name=server_name,
                        external_id=ext_id,
                        title=str(detail.get("title") or entry.get("title") or "Untitled"),
                        content=content,
                        source_url=detail.get("source_url") or entry.get("source_url"),
                        tags=entry.get("tags") or detail.get("tags") or [],
                    )
                    docs_upserted += 1
                    chunks_written += chunks
                except Exception as exc:
                    logger.exception("MCP doc ingest failed: %s", ext_id)
                    errors.append(f"{ext_id}: {exc}")
            cursor = listing.get("next_cursor")
            if not cursor:
                break

    return {
        "server": server_name,
        "docs_seen": docs_seen,
        "docs_upserted": docs_upserted,
        "chunks_written": chunks_written,
        "errors": errors[:20],  # cap so the response stays bounded
    }


async def _load_mcp_integration(
    db: AsyncSession, tenant_id: uuid.UUID, server_name: str
) -> Integration:
    """Find the MCP integration row for ``server_name``.

    Multiple MCP servers can be registered per tenant (e.g., separate
    servers for product docs vs. support macros). We disambiguate by
    ``provider_config.name``.
    """
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "mcp")
        .order_by(Integration.created_at.desc())
    )
    rows = list((await db.execute(stmt)).scalars().all())
    for row in rows:
        if (row.provider_config or {}).get("name") == server_name:
            return row
    if rows and server_name == "default":
        return rows[0]
    raise RuntimeError(f"No MCP integration named '{server_name}' for tenant")


async def _upsert_mcp_doc(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    server_name: str,
    external_id: str,
    title: str,
    content: str,
    source_url: Optional[str],
    tags: list,
) -> int:
    """Upsert a KBDocument keyed by ``(tenant, source_type='mcp:{name}',
    source_external_id)`` and embed it."""
    source_type = f"mcp:{server_name}"
    stmt = select(KBDocument).where(
        KBDocument.tenant_id == tenant_id,
        KBDocument.source_type == source_type,
        KBDocument.source_external_id == external_id,
    )
    doc = (await db.execute(stmt)).scalar_one_or_none()
    if doc is None:
        doc = KBDocument(
            tenant_id=tenant_id,
            title=title,
            content=content,
            source_type=source_type,
            source_url=source_url,
            source_external_id=external_id,
            tags=list(tags or []),
        )
        db.add(doc)
        await db.flush()
    else:
        doc.title = title or doc.title
        doc.content = content
        doc.source_url = source_url or doc.source_url
        if tags:
            doc.tags = list(tags)
        doc.last_synced_at = datetime.now(timezone.utc)

    return await ingest_document(db, doc)
