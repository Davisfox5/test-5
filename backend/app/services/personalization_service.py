"""Per-tenant prompt personalisation — context block + few-shot pool + RAG.

Layer 4 of the continuous-improvement system.  Builds optional blocks that
the producers append to the **user message** (not the system prompt) so the
shared system-prompt cache hit is preserved across tenants while behaviour
is still customised per tenant.

Five mechanisms:
1. Vocabulary + persona injection — :func:`build_analysis_context_block`
2. Few-shot pool — :func:`refresh_pools_all_tenants` + selection helpers
3. RAG into analysis pipeline — :func:`build_rag_context_block`
4. Reply tone examples — already lives in ``email_reply._tone_examples``
5. Parameter overrides — :func:`get_parameter_overrides`
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import (
    ActionItem,
    InsightQualityScore,
    Interaction,
    Tenant,
    TenantPromptConfig,
)

logger = logging.getLogger(__name__)

POOL_CAP = 15
PROMOTION_DAILY_CAP = 3
QUALITY_THRESHOLD = 0.85


# ── TenantPromptConfig accessor ──────────────────────────────────────────


def _get_or_create_config(session: Session, tenant_id: Any) -> TenantPromptConfig:
    """Lazy-create the per-tenant config row."""
    config = (
        session.query(TenantPromptConfig)
        .filter(TenantPromptConfig.tenant_id == tenant_id)
        .one_or_none()
    )
    if config is None:
        config = TenantPromptConfig(tenant_id=tenant_id)
        session.add(config)
        session.flush()
    return config


def get_config(session: Session, tenant: Tenant) -> Optional[TenantPromptConfig]:
    """Read-only fetch — returns None if no row exists yet (no DB write)."""
    return (
        session.query(TenantPromptConfig)
        .filter(TenantPromptConfig.tenant_id == tenant.id)
        .one_or_none()
    )


# ── Context block builders (called by ai_analysis worker) ────────────────


def _format_vocabulary_block(
    persona: Optional[str],
    custom_terms: List[str],
    acronyms: Dict[str, str],
) -> Optional[str]:
    if not persona and not custom_terms and not acronyms:
        return None
    lines: List[str] = ["## Tenant Context"]
    if persona:
        lines.append(f"Personas: {persona.strip()}")
    if custom_terms:
        lines.append(f"Domain terms: {', '.join(custom_terms[:50])}")
    if acronyms:
        acronym_lines = [f"  - {k} = {v}" for k, v in list(acronyms.items())[:50]]
        lines.append("Acronyms:\n" + "\n".join(acronym_lines))
    return "\n".join(lines)


def build_analysis_context_block(session: Session, tenant: Tenant) -> Optional[str]:
    """Tenant-context block to append to the analysis user message."""
    config = get_config(session, tenant)
    if config is None:
        # Fallback: use the tenant's keyterm_boost_list even without a config row.
        return _format_vocabulary_block(None, list(tenant.keyterm_boost_list or []), {})
    return _format_vocabulary_block(
        config.persona_block,
        list(config.custom_terms or []),
        dict(config.acronyms or {}),
    )


def build_classifier_context_block(session: Session, tenant: Tenant) -> Optional[str]:
    """Smaller context block for the email classifier — persona + acronyms only."""
    config = get_config(session, tenant)
    if config is None:
        return None
    if not config.persona_block and not config.acronyms:
        return None
    parts: List[str] = ["## Tenant Context"]
    if config.persona_block:
        parts.append(f"Persona: {config.persona_block.strip()}")
    if config.acronyms:
        parts.append(
            "Acronyms: "
            + ", ".join(f"{k}={v}" for k, v in list(config.acronyms.items())[:30])
        )
    return "\n".join(parts)


def build_reply_context_block(session: Session, tenant: Tenant) -> Optional[str]:
    """Tenant-context block injected into the reply drafter user message."""
    return build_analysis_context_block(session, tenant)


# ── RAG-into-analysis (Layer 4 mechanism #3) ─────────────────────────────


def build_rag_context_block(
    session: Session,
    tenant: Tenant,
    triage_result: Optional[Dict[str, Any]],
    channel: Optional[str] = None,
) -> Optional[str]:
    """Retrieve top-k KB chunks for this call's topics and format as context.

    Reuses the existing :func:`backend.app.services.kb_document_retrieval.retrieve`
    — same Voyage embeddings, same per-tenant Qdrant collection. Falls back
    to None on any failure so the analysis pipeline isn't blocked by a dead RAG.
    """
    config = get_config(session, tenant)
    rag_config = (config.rag_config if config else {}) or {}
    if not rag_config.get("analysis_enabled", False):
        return None

    topics = []
    if triage_result:
        topics = list(triage_result.get("topics") or [])
    quick_summary = (triage_result or {}).get("quick_summary", "")
    query = (" ".join(topics) + " " + quick_summary).strip()
    if not query:
        return None

    top_k = int(rag_config.get("top_k", 3))

    try:
        # kb_document_retrieval.retrieve is async; we need a sync entrypoint here
        # because the analysis worker runs synchronously inside Celery.
        # Use the sync keyword-fallback path — it's tenant-scoped and
        # requires no event loop.  When Voyage is unavailable this is also
        # what the async path falls back to.
        from backend.app.models import KBDocument
        from sqlalchemy import or_
        import re

        token_re = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
        tokens = [t.lower() for t in token_re.findall(query)][:8]
        if not tokens:
            return None
        clauses = []
        for tok in tokens:
            pattern = f"%{tok}%"
            clauses.append(KBDocument.content.ilike(pattern))
            clauses.append(KBDocument.title.ilike(pattern))
        rows = (
            session.query(KBDocument)
            .filter(KBDocument.tenant_id == tenant.id)
            .filter(or_(*clauses))
            .limit(50)
            .all()
        )
        if not rows:
            return None

        # Quick TF rescore in Python (mirrors kb_document_retrieval._keyword_ranker)
        scored: List[tuple] = []
        for doc in rows:
            haystack = [
                t.lower()
                for t in token_re.findall((doc.title or "") + " " + (doc.content or ""))
            ]
            if not haystack:
                continue
            counts: Dict[str, int] = {}
            for t in haystack:
                counts[t] = counts.get(t, 0) + 1
            score = sum(1 for q in tokens if counts.get(q))
            if score > 0:
                scored.append((doc, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        chosen = scored[:top_k]
        if not chosen:
            return None
        sections = ["## Relevant Knowledge"]
        for doc, _score in chosen:
            sections.append(
                f"### {doc.title or 'Untitled'} (id={doc.id})\n{(doc.content or '')[:1500]}"
            )
        return "\n\n".join(sections)
    except Exception:
        logger.exception("RAG context build failed (non-fatal)")
        return None


# ── Parameter overrides ──────────────────────────────────────────────────


def get_parameter_overrides(
    session: Session, tenant: Tenant, surface: str
) -> Dict[str, Any]:
    """Read per-tenant parameter overrides for the given surface."""
    config = get_config(session, tenant)
    if config is None:
        return {}
    overrides = dict(config.parameter_overrides or {})
    surface_overrides = overrides.get(surface) or {}
    # Surface-specific overrides win over global ones.
    merged: Dict[str, Any] = {
        k: v for k, v in overrides.items() if not isinstance(v, dict)
    }
    merged.update(surface_overrides if isinstance(surface_overrides, dict) else {})
    return merged


# ── Few-shot pool (Layer 4 mechanism #2) ─────────────────────────────────


def _is_already_in_pool(pool: List[Dict[str, Any]], interaction_id: str) -> bool:
    return any(item.get("interaction_id") == interaction_id for item in pool)


def _promote_for_tenant(session: Session, tenant: Tenant) -> int:
    """Promote up to ``PROMOTION_DAILY_CAP`` analysis examples for one tenant."""
    config = _get_or_create_config(session, tenant.id)
    pool: Dict[str, List[Dict[str, Any]]] = dict(config.few_shot_pool or {})
    analysis_pool: List[Dict[str, Any]] = list(pool.get("analysis", []))

    # Find candidates: high quality_score AND at least one done action_item.
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = (
        session.query(
            Interaction.id,
            Interaction.insights,
        )
        .join(InsightQualityScore, InsightQualityScore.interaction_id == Interaction.id)
        .filter(Interaction.tenant_id == tenant.id)
        .filter(Interaction.created_at >= cutoff)
        .filter(InsightQualityScore.score >= QUALITY_THRESHOLD)
        .order_by(Interaction.created_at.desc())
        .limit(50)
        .all()
    )

    promoted = 0
    for interaction_id, insights in rows:
        if promoted >= PROMOTION_DAILY_CAP:
            break
        iid = str(interaction_id)
        if _is_already_in_pool(analysis_pool, iid):
            continue
        # Confirm it has at least one done action item (cheap follow-up check).
        has_done = (
            session.query(ActionItem.id)
            .filter(
                ActionItem.interaction_id == interaction_id,
                ActionItem.status.in_(("done", "completed")),
            )
            .first()
            is not None
        )
        if not has_done:
            continue
        analysis_pool.insert(
            0,
            {
                "interaction_id": iid,
                "summary": (insights or {}).get("summary", "")[:500],
                "action_items": (insights or {}).get("action_items", [])[:5],
                "promoted_at": datetime.utcnow().isoformat(),
            },
        )
        promoted += 1

    # Cap pool size; oldest evicted.
    analysis_pool = analysis_pool[:POOL_CAP]
    pool["analysis"] = analysis_pool
    config.few_shot_pool = pool
    return promoted


def refresh_pools_all_tenants(session: Session) -> Dict[str, Any]:
    """Nightly Celery Beat entrypoint."""
    tenants = session.query(Tenant).all()
    total = 0
    for tenant in tenants:
        try:
            total += _promote_for_tenant(session, tenant)
        except Exception:
            logger.exception("Few-shot promotion failed for tenant %s", tenant.id)
    session.commit()
    return {"tenants_processed": len(tenants), "examples_promoted": total}


def select_few_shot_examples(
    session: Session, tenant: Tenant, surface: str, limit: int = 3
) -> List[Dict[str, Any]]:
    """Return a slice of the tenant's few-shot pool for inference-time injection.

    For now uses recency ordering (the pool is already sorted newest-first).
    A future iteration should re-rank by Qdrant similarity to the current
    item — the helper is in place for that swap.
    """
    config = get_config(session, tenant)
    if config is None:
        return []
    pool = (config.few_shot_pool or {}).get(surface, [])
    return list(pool[:limit])
