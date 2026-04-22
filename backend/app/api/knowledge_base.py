"""Knowledge Base API — document management, upload, search, and external sync."""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal, get_current_tenant
from backend.app.db import get_db
from backend.app.models import Contact, KBChunk, KBDocument, PinnedKBCard, Tenant
from backend.app.services.kb import RetrievalService, ingest_document, reindex_tenant
from backend.app.services.kb.context_dispatch import schedule_context_rebuild
from backend.app.services.kb.embedder import VoyageEmbedderError
from backend.app.services.kb.extractors import ExtractionError, extract_text
from backend.app.services.kb.vector_store import get_vector_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class KBDocCreate(BaseModel):
    title: str
    content: str
    tags: Optional[List[str]] = None
    source_type: str = "editor"


class KBDocOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    title: Optional[str]
    content: Optional[str]
    source_type: Optional[str]
    source_url: Optional[str]
    tags: list
    last_synced_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class KBDocUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None


# ── Endpoints ────────────────────────────────────────────


@router.get("/kb/docs", response_model=List[KBDocOut])
async def list_kb_docs(
    source_type: Optional[str] = Query(None, description="Filter by source type: editor, upload, confluence, notion, gdrive"),
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter by"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(KBDocument)
        .where(KBDocument.tenant_id == tenant.id)
        .order_by(KBDocument.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if source_type:
        stmt = stmt.where(KBDocument.source_type == source_type)
    if tags:
        # Filter docs that contain any of the requested tags
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        for tag in tag_list:
            stmt = stmt.where(KBDocument.tags.contains([tag]))

    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/kb/docs/{doc_id}", response_model=KBDocOut)
async def get_kb_doc(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(KBDocument).where(KBDocument.id == doc_id, KBDocument.tenant_id == tenant.id)
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.post("/kb/docs", response_model=KBDocOut, status_code=201)
async def create_kb_doc(
    body: KBDocCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    doc = KBDocument(
        tenant_id=tenant.id,
        title=body.title,
        content=body.content,
        source_type=body.source_type,
        tags=body.tags or [],
    )
    db.add(doc)
    await db.flush()

    try:
        await ingest_document(db, doc)
    except VoyageEmbedderError:
        # Surface the failure to the client so they know retrieval won't work,
        # but the doc row is already saved — they can retry via /kb/docs/{id}/reindex.
        logger.exception("Failed to embed new KB doc %s", doc.id)
        raise HTTPException(
            status_code=502,
            detail="Document saved, but embedding failed. Retry reindex when available.",
        )
    await schedule_context_rebuild(tenant.id)
    return doc


@router.put("/kb/docs/{doc_id}", response_model=KBDocOut)
async def update_kb_doc(
    doc_id: uuid.UUID,
    body: KBDocUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(KBDocument).where(KBDocument.id == doc_id, KBDocument.tenant_id == tenant.id)
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    content_changed = body.content is not None and body.content != doc.content

    if body.title is not None:
        doc.title = body.title
    if body.content is not None:
        doc.content = body.content
    if body.tags is not None:
        doc.tags = body.tags

    if content_changed:
        try:
            await ingest_document(db, doc)
        except VoyageEmbedderError:
            logger.exception("Failed to re-embed updated KB doc %s", doc.id)
            raise HTTPException(
                status_code=502,
                detail="Document saved, but re-embedding failed. Retry reindex when available.",
            )
        await schedule_context_rebuild(tenant.id)

    return doc


@router.delete("/kb/docs/{doc_id}", status_code=204)
async def delete_kb_doc(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(KBDocument).where(KBDocument.id == doc_id, KBDocument.tenant_id == tenant.id)
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # For Qdrant, the Postgres FK cascade doesn't reach the vector store — we
    # have to clean it up explicitly. For pgvector this is a no-op since the
    # cascade on kb_chunks already drops the vectors.
    try:
        await get_vector_store().delete_doc(db, tenant.id, doc.id)
    except Exception:
        logger.exception("Vector store delete_doc failed for %s — row delete continues", doc.id)

    await db.delete(doc)
    # Schedule a *full* rebuild on delete — the incremental merge prompt can
    # only add/update facts, not retract them. A full rebuild reflects the
    # deletion by re-summarizing the remaining docs.
    await schedule_context_rebuild(tenant.id, full=True)


@router.post("/kb/upload", response_model=KBDocOut, status_code=201)
async def upload_kb_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Upload a file and extract text content to store as a KB document.

    Currently supports .txt files. PDF and DOCX extraction will be added.
    """
    filename = file.filename or "untitled"
    content_bytes = await file.read()

    if len(content_bytes) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(status_code=400, detail="File too large (max 50MB)")

    try:
        content = extract_text(filename, content_bytes)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    doc = KBDocument(
        tenant_id=tenant.id,
        title=filename,
        content=content,
        source_type="upload",
        source_url=None,
        tags=[],
    )
    db.add(doc)
    await db.flush()

    try:
        await ingest_document(db, doc)
    except VoyageEmbedderError:
        logger.exception("Failed to embed uploaded KB doc %s", doc.id)
        raise HTTPException(
            status_code=502,
            detail="Document saved, but embedding failed. Retry reindex when available.",
        )
    await schedule_context_rebuild(tenant.id)
    return doc


@router.post("/kb/docs/{doc_id}/reindex", status_code=202)
async def reindex_kb_doc(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Re-embed a single document. Useful when a prior embed failed."""
    stmt = select(KBDocument).where(
        KBDocument.id == doc_id, KBDocument.tenant_id == tenant.id
    )
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    chunks = await ingest_document(db, doc, force=True)
    return {"doc_id": str(doc_id), "chunks_written": chunks}


@router.post("/kb/reindex", status_code=202)
async def reindex_all_kb(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Re-embed every document for the tenant. Use after a backend swap."""
    total = await reindex_tenant(db, tenant.id, force=True)
    await schedule_context_rebuild(tenant.id, full=True)
    return {"tenant_id": str(tenant.id), "chunks_written": total}


class KBSearchHitOut(BaseModel):
    chunk_id: str
    doc_id: str
    chunk_idx: int
    text: str
    score: float
    doc_title: Optional[str] = None
    source_url: Optional[str] = None


@router.get("/kb/search", response_model=List[KBSearchHitOut])
async def search_kb(
    query: str = Query(..., min_length=1, description="Natural-language query"),
    limit: int = Query(5, le=20),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Semantic search over embedded KB chunks.

    Falls back to a SQL ILIKE scan if the embedder is unavailable, so callers
    still get something useful when Voyage is down.
    """
    service = RetrievalService()
    hits = await service.search(db, tenant.id, query, k=limit)
    if hits:
        return [
            KBSearchHitOut(
                chunk_id=str(h.chunk_id),
                doc_id=str(h.doc_id),
                chunk_idx=h.chunk_idx,
                text=h.text,
                score=h.score,
                doc_title=h.doc_title,
                source_url=h.source_url,
            )
            for h in hits
        ]

    # Fallback: keyword match when we have no embeddings or the embedder failed.
    pattern = f"%{query}%"
    stmt = (
        select(KBDocument)
        .where(
            KBDocument.tenant_id == tenant.id,
            KBDocument.content.ilike(pattern) | KBDocument.title.ilike(pattern),
        )
        .limit(limit)
    )
    rows = await db.execute(stmt)
    docs = rows.scalars().all()
    return [
        KBSearchHitOut(
            chunk_id=str(d.id),
            doc_id=str(d.id),
            chunk_idx=0,
            text=(d.content or "")[:400],
            score=0.0,
            doc_title=d.title,
            source_url=d.source_url,
        )
        for d in docs
    ]


class PinRequest(BaseModel):
    contact_id: uuid.UUID
    chunk_id: uuid.UUID


class PinOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    doc_id: uuid.UUID
    chunk_id: uuid.UUID
    pinned_at: datetime

    model_config = {"from_attributes": True}


class PinnedCardOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    doc_id: uuid.UUID
    chunk_id: uuid.UUID
    pinned_at: datetime
    chunk_text: str
    doc_title: Optional[str] = None
    source_url: Optional[str] = None


@router.post("/kb/pins", response_model=PinOut, status_code=201)
async def pin_card(
    body: PinRequest,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Pin a KB chunk for a contact so it carries across calls."""
    tenant = principal.tenant
    chunk = await db.get(KBChunk, body.chunk_id)
    if not chunk or chunk.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Chunk not found")
    contact = await db.get(Contact, body.contact_id)
    if not contact or contact.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Contact not found")

    existing = await db.execute(
        select(PinnedKBCard).where(
            PinnedKBCard.tenant_id == tenant.id,
            PinnedKBCard.contact_id == body.contact_id,
            PinnedKBCard.chunk_id == body.chunk_id,
        )
    )
    pin = existing.scalar_one_or_none()
    if pin is None:
        pin = PinnedKBCard(
            tenant_id=tenant.id,
            contact_id=body.contact_id,
            doc_id=chunk.doc_id,
            chunk_id=body.chunk_id,
            # Audit — which agent pinned this card.
            pinned_by_user_id=principal.user_id,
        )
        db.add(pin)
        await db.flush()
    return pin


@router.delete("/kb/pins/{pin_id}", status_code=204)
async def unpin_card(
    pin_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    pin = await db.get(PinnedKBCard, pin_id)
    if not pin or pin.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Pin not found")
    await db.delete(pin)


@router.get("/kb/pins", response_model=List[PinnedCardOut])
async def list_pins_for_contact(
    contact_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """List pinned KB cards for a contact, with chunk text inlined so the
    frontend can rehydrate the sidebar when a new call starts."""
    stmt = (
        select(PinnedKBCard, KBChunk, KBDocument)
        .join(KBChunk, KBChunk.id == PinnedKBCard.chunk_id)
        .join(KBDocument, KBDocument.id == PinnedKBCard.doc_id)
        .where(
            PinnedKBCard.tenant_id == tenant.id,
            PinnedKBCard.contact_id == contact_id,
        )
        .order_by(PinnedKBCard.pinned_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    out: List[PinnedCardOut] = []
    for pin, chunk, doc in rows:
        out.append(
            PinnedCardOut(
                id=pin.id,
                contact_id=pin.contact_id,
                doc_id=pin.doc_id,
                chunk_id=pin.chunk_id,
                pinned_at=pin.pinned_at,
                chunk_text=chunk.text,
                doc_title=doc.title,
                source_url=doc.source_url,
            )
        )
    return out


@router.post("/kb/sync/{provider}", status_code=202)
async def sync_kb_provider(
    provider: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Trigger a KB sync for an external provider.

    Supported providers: ``gdrive``, ``onedrive``, ``sharepoint``,
    ``confluence``. The tenant must have an ``Integration`` row for the
    provider with OAuth credentials (Google/Microsoft) or API creds
    (Confluence basic or bearer) already saved.
    """
    from backend.app.services.kb.sync_runner import SUPPORTED_PROVIDERS

    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid provider '{provider}'. Must be one of: "
                + ", ".join(sorted(SUPPORTED_PROVIDERS))
            ),
        )

    try:
        from backend.app.tasks import sync_knowledge_base

        sync_knowledge_base.delay(str(tenant.id), provider)
    except Exception:
        logger.exception(
            "Failed to dispatch KB sync task for tenant=%s provider=%s",
            tenant.id,
            provider,
        )

    return JSONResponse(
        status_code=202,
        content={
            "message": f"Sync queued for provider '{provider}'. Documents will land shortly.",
            "provider": provider,
        },
    )


# ── Third-party direct ingest ─────────────────────────────────────────


class KBExternalIngestIn(BaseModel):
    """One document pushed into the KB by a third-party system.

    Used by customers who want to feed LINDA from their own stack
    without us maintaining a bespoke adapter. Identify documents with
    ``(source_type, source_external_id)`` so re-pushes upsert.
    """

    source_type: str  # e.g., "vendor-docs", "runbooks", "zendesk-macros"
    source_external_id: str
    title: str
    content: str
    source_url: Optional[str] = None
    tags: Optional[List[str]] = None


@router.post("/kb/ingest", status_code=202)
async def ingest_external_document(
    body: KBExternalIngestIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Accept one document from a third-party system and embed it
    synchronously. The caller identifies the doc with
    ``(source_type, source_external_id)`` so re-posting the same pair
    updates in place.
    """
    stmt = select(KBDocument).where(
        KBDocument.tenant_id == tenant.id,
        KBDocument.source_type == body.source_type,
        KBDocument.source_external_id == body.source_external_id,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is None:
        doc = KBDocument(
            tenant_id=tenant.id,
            title=body.title,
            content=body.content,
            source_type=body.source_type,
            source_url=body.source_url,
            source_external_id=body.source_external_id,
            tags=list(body.tags or []),
        )
        db.add(doc)
        await db.flush()
    else:
        doc = existing
        doc.title = body.title or doc.title
        doc.content = body.content
        doc.source_url = body.source_url or doc.source_url
        if body.tags is not None:
            doc.tags = list(body.tags)
        doc.last_synced_at = datetime.utcnow()

    try:
        chunks = await ingest_document(db, doc)
    except VoyageEmbedderError as exc:
        raise HTTPException(status_code=502, detail=f"embedding failed: {exc}")

    return {
        "doc_id": str(doc.id),
        "chunks_written": chunks,
        "source_type": doc.source_type,
        "source_external_id": doc.source_external_id,
    }


# ── MCP-based ingest ──────────────────────────────────────────────────


class KBMcpIngestIn(BaseModel):
    """Trigger a one-shot pull from a registered MCP server.

    The MCP server must implement a ``kb/list`` + ``kb/get`` pair that
    mirrors the ``ExternalDocument`` shape. We call it over HTTP; the
    server endpoint + shared secret live in the tenant's Integration
    row with provider='mcp'.
    """

    server_name: str = (
        "default"  # which MCP server to pull from when multiple are linked
    )


@router.post("/kb/mcp/sync", status_code=202)
async def sync_kb_via_mcp(
    body: KBMcpIngestIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Pull documents from an MCP server registered for this tenant.

    The adapter reads ``Integration(provider='mcp', provider_config.name=
    body.server_name)`` for the endpoint URL + bearer secret, then calls
    the server's ``kb/list`` tool repeatedly and ingests each document.
    """
    from backend.app.services.kb.providers.mcp import pull_from_mcp

    try:
        summary = await pull_from_mcp(
            db, tenant_id=tenant.id, server_name=body.server_name
        )
    except Exception as exc:
        logger.exception("MCP pull failed (%s)", body.server_name)
        raise HTTPException(status_code=502, detail=str(exc))
    return summary
