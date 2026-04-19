"""Knowledge Base API — document management, upload, search, and external sync."""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import KBDocument, Tenant
from backend.app.services.kb_retrieval import (
    delete_from_index,
    index_document,
    retrieve,
)

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
    await index_document(db, doc)
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

    if body.title is not None:
        doc.title = body.title
    if body.content is not None:
        doc.content = body.content
    if body.tags is not None:
        doc.tags = body.tags

    await index_document(db, doc)
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
    await delete_from_index(tenant.id, doc.id)
    await db.delete(doc)


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

    # Determine file type and extract text
    lower_name = filename.lower()
    if lower_name.endswith(".txt"):
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = content_bytes.decode("latin-1")
    elif lower_name.endswith(".pdf"):
        # TODO: Integrate PDF text extraction (e.g., PyMuPDF / pdfplumber)
        raise HTTPException(status_code=400, detail="PDF extraction not yet implemented — coming soon")
    elif lower_name.endswith(".docx"):
        # TODO: Integrate DOCX text extraction (e.g., python-docx)
        raise HTTPException(status_code=400, detail="DOCX extraction not yet implemented — coming soon")
    else:
        # Try to read as plain text
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Unsupported file type or encoding")

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
    await index_document(db, doc)
    return doc


@router.get("/kb/search", response_model=List[KBDocOut])
async def search_kb(
    query: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Search KB docs — Qdrant vector search when available, keyword fallback otherwise."""
    ranked = await retrieve(db, tenant.id, query, k=limit)
    return [doc for doc, _score in ranked]


@router.post("/kb/sync/{provider}", status_code=202)
async def sync_kb_provider(
    provider: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Trigger a knowledge base sync for an external provider.

    Supported providers: confluence, notion, gdrive.
    This is a placeholder — actual sync will be handled by a background worker.
    """
    valid_providers = {"confluence", "notion", "gdrive"}
    if provider not in valid_providers:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider '{provider}'. Must be one of: {', '.join(sorted(valid_providers))}",
        )

    # TODO: Dispatch background task to sync from provider using stored OAuth credentials
    # sync_knowledge_base.delay(str(tenant.id), provider)

    return JSONResponse(
        status_code=202,
        content={"message": f"Sync triggered for provider '{provider}'. Documents will be updated shortly."},
    )
