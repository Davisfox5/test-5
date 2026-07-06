"""Doc-level KB retrieval — the maintained vector store, keyword fallback.

The reply drafter calls :func:`retrieve` with a free-form query (the
email thread's most recent message + subject).  We return a ranked list
of ``(KBDocument, score)`` tuples; the caller decides how many to paste
into the Sonnet prompt.

Vector search goes through :class:`~backend.app.services.kb.retrieval.
RetrievalService` — the single choke point over ``kb/vector_store.py``
(pgvector by default, the shared-collection Qdrant backend when
``VECTOR_BACKEND=qdrant``), which injects the tenant filter on every
query. This module previously carried its OWN Qdrant client writing to
per-tenant ``kb_tenant_{id}`` collections that ingestion never populated
— a dead vector path that silently keyword-fell-back on every call, and
a second unmanaged client outside the tenant-isolation choke point
(docs/complexity/04-tenant-isolation-migration.md §5). Retired 2026-07.

If embeddings/vector search are unavailable we degrade gracefully to a
PostgreSQL keyword ranker.  That fallback is deterministic,
tenant-scoped, and — critically — never introduces cross-tenant leakage.
"""

from __future__ import annotations

import logging
import math
import re
from typing import List, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import KBDocument

logger = logging.getLogger(__name__)


async def retrieve(
    db: AsyncSession, tenant_id, query: str, k: int = 5
) -> List[Tuple[KBDocument, float]]:
    """Return ranked KB docs for a query, tenant-scoped.

    Tries the maintained vector path first (chunk-level hits collapsed to
    their parent documents, best chunk score wins).  Falls back to the
    keyword ranker on any failure or empty result — good enough to
    surface the right doc on small corpora and never hallucinates a
    cross-tenant hit.
    """
    try:
        from backend.app.services.kb.retrieval import RetrievalService

        hits = await RetrievalService().search(db, tenant_id, query, k=k * 3)
        if hits:
            doc_ids = {h.doc_id for h in hits}
            rows = (
                await db.execute(
                    select(KBDocument).where(
                        KBDocument.tenant_id == tenant_id,
                        KBDocument.id.in_(doc_ids),
                    )
                )
            ).scalars().all()
            by_id = {r.id: r for r in rows}
            ranked: List[Tuple[KBDocument, float]] = []
            seen = set()
            for h in hits:  # hits arrive score-descending
                if h.doc_id in seen or h.doc_id not in by_id:
                    continue
                seen.add(h.doc_id)
                ranked.append((by_id[h.doc_id], float(h.score)))
            if ranked:
                return ranked[:k]
    except Exception:
        logger.exception("KB vector retrieval failed; falling back to keyword ranker")

    return await _keyword_ranker(db, tenant_id, query, k)


# ── Keyword fallback ────────────────────────────────────


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


async def _keyword_ranker(
    db: AsyncSession, tenant_id, query: str, k: int
) -> List[Tuple[KBDocument, float]]:
    """Very small TF ranker — pulls a larger candidate set via ILIKE on
    the top-N most distinctive query tokens and rescores in Python.
    """
    tokens = _tokenize(query)[:8]
    if not tokens:
        rows = (await db.execute(
            select(KBDocument)
            .where(KBDocument.tenant_id == tenant_id)
            .order_by(KBDocument.last_synced_at.desc().nullslast())
            .limit(k)
        )).scalars().all()
        return [(r, 0.0) for r in rows]

    clauses = []
    for tok in tokens:
        pattern = f"%{tok}%"
        clauses.append(KBDocument.content.ilike(pattern))
        clauses.append(KBDocument.title.ilike(pattern))

    candidates = (
        await db.execute(
            select(KBDocument)
            .where(KBDocument.tenant_id == tenant_id, or_(*clauses))
            .limit(50)
        )
    ).scalars().all()

    scored: List[Tuple[KBDocument, float]] = []
    for doc in candidates:
        haystack = _tokenize((doc.title or "") + " " + (doc.content or ""))
        if not haystack:
            continue
        counts: dict[str, int] = {}
        for t in haystack:
            counts[t] = counts.get(t, 0) + 1
        score = 0.0
        for q in tokens:
            score += math.log(1 + counts.get(q, 0))
        if score > 0:
            scored.append((doc, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
