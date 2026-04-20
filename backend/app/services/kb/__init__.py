"""Knowledge-base ingestion and retrieval services."""

from backend.app.services.kb.chunker import chunk_text
from backend.app.services.kb.context_builder import (
    ContextBuilderService,
    format_brief_for_prompt,
)
from backend.app.services.kb.embedder import VoyageEmbedder
from backend.app.services.kb.ingest import ingest_document, reindex_tenant
from backend.app.services.kb.retrieval import RetrievalService
from backend.app.services.kb.vector_store import (
    ChunkRecord,
    SearchHit,
    VectorStore,
    get_vector_store,
)

__all__ = [
    "chunk_text",
    "VoyageEmbedder",
    "ingest_document",
    "reindex_tenant",
    "RetrievalService",
    "ChunkRecord",
    "SearchHit",
    "VectorStore",
    "get_vector_store",
    "ContextBuilderService",
    "format_brief_for_prompt",
]
