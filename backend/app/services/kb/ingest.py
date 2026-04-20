"""KB document ingestion — chunk, embed, store.

Entry points:

* ``ingest_document`` — embed one document; used on create/update.
* ``reindex_tenant`` — re-embed every document for a tenant; used after a
  backend switch (pgvector ⇄ qdrant) or embedding model change.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import KBChunk, KBDocument
from backend.app.services.kb.chunker import approx_token_count, chunk_text
from backend.app.services.kb.embedder import VoyageEmbedder, VoyageEmbedderError
from backend.app.services.kb.vector_store import (
    ChunkRecord,
    VectorStore,
    get_vector_store,
)

logger = logging.getLogger(__name__)


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def ingest_document(
    db: AsyncSession,
    doc: KBDocument,
    *,
    embedder: Optional[VoyageEmbedder] = None,
    store: Optional[VectorStore] = None,
    force: bool = False,
) -> int:
    """Chunk, embed, and store a single document.

    No-op when the content hash hasn't changed and ``force`` is False.

    Returns the number of chunks written.
    """
    if not doc.content or not doc.content.strip():
        logger.info("Skipping embed for doc %s — empty content", doc.id)
        return 0

    new_hash = _hash_content(doc.content)
    if not force and doc.content_hash == new_hash and doc.embedded_at is not None:
        return 0

    settings = get_settings()
    embedder = embedder or VoyageEmbedder()
    store = store or get_vector_store()

    pieces = chunk_text(
        doc.content,
        target_tokens=settings.KB_CHUNK_TOKENS,
        overlap_tokens=settings.KB_CHUNK_OVERLAP_TOKENS,
    )
    if not pieces:
        return 0

    # Drop any prior chunks for this doc before writing new ones.
    await store.delete_doc(db, doc.tenant_id, doc.id)
    await db.execute(
        KBChunk.__table__.delete().where(KBChunk.doc_id == doc.id)
    )

    try:
        embeddings = await embedder.embed(pieces, input_type="document")
    except VoyageEmbedderError as exc:
        logger.error("Voyage embed failed for doc %s: %s", doc.id, exc)
        raise

    records = []
    for idx, (piece, vec) in enumerate(zip(pieces, embeddings)):
        chunk_id = uuid.uuid4()
        db.add(
            KBChunk(
                id=chunk_id,
                tenant_id=doc.tenant_id,
                doc_id=doc.id,
                chunk_idx=idx,
                text=piece,
                token_count=approx_token_count(piece),
                content_hash=_hash_content(piece),
            )
        )
        records.append(
            ChunkRecord(
                id=chunk_id,
                tenant_id=doc.tenant_id,
                doc_id=doc.id,
                chunk_idx=idx,
                text=piece,
                embedding=vec,
                doc_title=doc.title,
                source_url=doc.source_url,
            )
        )

    await store.upsert(db, records)

    doc.content_hash = new_hash
    doc.embedded_at = datetime.now(timezone.utc)

    logger.info("Ingested doc %s: %d chunks", doc.id, len(records))
    return len(records)


async def reindex_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    force: bool = True,
) -> int:
    """Re-ingest every document for a tenant. Returns total chunks written."""
    stmt = select(KBDocument).where(KBDocument.tenant_id == tenant_id)
    result = await db.execute(stmt)
    docs = list(result.scalars().all())

    embedder = VoyageEmbedder()
    store = get_vector_store()

    total = 0
    for doc in docs:
        total += await ingest_document(db, doc, embedder=embedder, store=store, force=force)
    return total
