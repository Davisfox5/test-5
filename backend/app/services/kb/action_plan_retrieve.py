"""KB retrieval tailored for the Action Plan synthesizer.

Two-pass retrieval:

* **Procedure pass** - looks up ``procedure``-kind chunks whose triggers
  match the call. Then, per the locked decision, strips required_steps
  that reference unconnected integrations. If all steps drop out, the
  procedure is dropped entirely - the synthesizer never sees an
  un-actionable procedure.

* **Reference pass** - standard vector search for everything else
  (policy / escalation / template / context / faq / glossary /
  contact_directory) so Call A has factual grounding alongside the
  authoritative procedures.

This sits beside the existing :mod:`backend.app.services.kb.retrieval`
which still serves the general live-coaching surface; the action plan
needs its own retriever because the gating + kind-aware shape differ.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Integration, KBChunk
from backend.app.services.kb.embedder import VoyageEmbedder, VoyageEmbedderError
from backend.app.services.kb.vector_store import (
    SearchHit,
    VectorStore,
    get_vector_store,
)

logger = logging.getLogger(__name__)


# Per-pass top-K. Procedures are authoritative so we pull more; reference
# articles are flavor and shouldn't crowd Call A's context.
DEFAULT_PROCEDURE_TOP_K = 6
DEFAULT_REFERENCE_TOP_K = 8


@dataclass
class RetrievedProcedure:
    """A procedure-kind chunk that survived the integration-capability gate."""

    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    doc_title: Optional[str]
    title: str
    content: str
    score: float
    # Per-kind metadata from the orchestrator. The synthesizer needs:
    # triggers, applies_when, required_steps (after gating),
    # required_integrations (after gating), compliance_level.
    metadata: Dict[str, Any] = field(default_factory=dict)
    compliance_level: str = "should"
    # Steps stripped because their target integration isn't connected,
    # surfaced to the admin alignment report (the engine never emits
    # them, but knowing the procedure was partially gated is useful).
    stripped_step_titles: List[str] = field(default_factory=list)


@dataclass
class RetrievedReference:
    """A non-procedure chunk - context, policy, escalation, etc."""

    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    doc_title: Optional[str]
    kind: str
    title: str
    content: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionPlanRetrievalResult:
    procedures: List[RetrievedProcedure] = field(default_factory=list)
    references: List[RetrievedReference] = field(default_factory=list)
    connected_providers: List[str] = field(default_factory=list)


class ActionPlanRetriever:
    """Procedure-aware retrieval for the Action Plan synthesizer."""

    def __init__(
        self,
        embedder: Optional[VoyageEmbedder] = None,
        store: Optional[VectorStore] = None,
    ) -> None:
        self._embedder = embedder or VoyageEmbedder()
        self._store = store or get_vector_store()

    async def retrieve(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        query: str,
        domain: str,
        procedure_top_k: int = DEFAULT_PROCEDURE_TOP_K,
        reference_top_k: int = DEFAULT_REFERENCE_TOP_K,
    ) -> ActionPlanRetrievalResult:
        """Retrieve procedures + reference chunks for a plan synthesis.

        ``query`` is typically the triage quick_summary + key topics
        concatenated - a phrase like "Customer requests refund for
        duplicate charge; mentions cancelling subscription". The
        ``domain`` is informational only here; the integration gate is
        what drives filtering.
        """
        connected = await _connected_provider_set(db, tenant_id)

        query = (query or "").strip()
        if not query:
            return ActionPlanRetrievalResult(
                connected_providers=sorted(connected),
            )

        try:
            vecs = await self._embedder.embed([query], input_type="query")
        except VoyageEmbedderError:
            logger.exception("Voyage embed failed for action plan retrieval")
            return ActionPlanRetrievalResult(
                connected_providers=sorted(connected),
            )
        if not vecs:
            return ActionPlanRetrievalResult(
                connected_providers=sorted(connected),
            )
        query_vec = vecs[0]

        # Wider pull from the store, then filter by kind locally so we
        # don't need to push a kind filter into every backend. Both pulls
        # are bounded by max(top_k_*) * 3 which is still cheap.
        raw_hits = await self._store.search(
            db,
            tenant_id=tenant_id,
            query_embedding=query_vec,
            k=max(procedure_top_k, reference_top_k) * 4,
        )
        if not raw_hits:
            return ActionPlanRetrievalResult(
                connected_providers=sorted(connected),
            )

        # Hydrate chunks so we can read kind + extracted_metadata.
        chunk_ids = [h.chunk_id for h in raw_hits]
        chunks_by_id = await _load_chunks(db, tenant_id, chunk_ids)

        procedures: List[RetrievedProcedure] = []
        references: List[RetrievedReference] = []
        for hit in raw_hits:
            chunk = chunks_by_id.get(hit.chunk_id)
            if chunk is None:
                continue
            kind = chunk.kind or "context"
            if kind == "procedure" and len(procedures) < procedure_top_k:
                gated = _gate_procedure(chunk, hit, connected)
                if gated is not None:
                    procedures.append(gated)
            elif kind != "procedure" and len(references) < reference_top_k:
                references.append(_build_reference(chunk, hit))

            if (
                len(procedures) >= procedure_top_k
                and len(references) >= reference_top_k
            ):
                break

        return ActionPlanRetrievalResult(
            procedures=procedures,
            references=references,
            connected_providers=sorted(connected),
        )


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────


async def _connected_provider_set(
    db: AsyncSession, tenant_id: uuid.UUID,
) -> set:
    rows = await db.execute(
        select(Integration.provider).where(Integration.tenant_id == tenant_id)
    )
    return {row[0] for row in rows.all() if row[0]}


async def _load_chunks(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    chunk_ids: Sequence[uuid.UUID],
) -> Dict[uuid.UUID, KBChunk]:
    if not chunk_ids:
        return {}
    rows = await db.execute(
        select(KBChunk).where(
            KBChunk.tenant_id == tenant_id,
            KBChunk.id.in_(list(chunk_ids)),
        )
    )
    return {c.id: c for c in rows.scalars()}


def _gate_procedure(
    chunk: KBChunk,
    hit: SearchHit,
    connected_providers: set,
) -> Optional[RetrievedProcedure]:
    """Strip steps requiring unconnected providers; drop procedure if empty.

    Implements the locked decision: "If HubSpot isn't connected, the
    system shouldn't even consider suggesting an action involving
    HubSpot." Steps that target unconnected integrations are removed
    here so Call A never sees them.

    Returns None when every required_step gets stripped (no actionable
    content left).
    """
    meta_orig = chunk.extracted_metadata or {}
    if not isinstance(meta_orig, dict):
        return None
    meta = dict(meta_orig)

    required_steps_in = meta.get("required_steps") or []
    if not isinstance(required_steps_in, list):
        required_steps_in = []

    required_ints = meta.get("required_integrations") or []
    if not isinstance(required_ints, list):
        required_ints = []

    # Two-pass strip: (1) integration-level — keep only required_ints
    # whose provider is connected; (2) step-level — most procedures don't
    # bind a specific step to a specific integration via field structure,
    # so we just drop the integration entries and let Call A know which
    # ints survived. Steps that explicitly reference a stripped provider
    # in their description (string match) get a per-step note appended.
    surviving_ints = [
        i for i in required_ints
        if isinstance(i, dict)
        and str(i.get("provider") or "").strip().lower() in connected_providers
    ]
    stripped_ints = [
        i for i in required_ints
        if isinstance(i, dict)
        and str(i.get("provider") or "").strip().lower() not in connected_providers
    ]
    stripped_provider_names = {
        str(i.get("provider") or "").strip().lower()
        for i in stripped_ints
    }

    surviving_steps: List[Dict[str, Any]] = []
    stripped_titles: List[str] = []
    for step in required_steps_in:
        if not isinstance(step, dict):
            continue
        # If the step description / title explicitly names a stripped
        # provider, drop the step. This is a coarse string check; the
        # orchestrator schema doesn't bind steps to integrations
        # structurally, but in practice step language usually mentions
        # the system ("Log refund in NetSuite").
        haystack = (
            str(step.get("title") or "")
            + " "
            + str(step.get("description") or "")
        ).lower()
        if any(p and p in haystack for p in stripped_provider_names):
            stripped_titles.append(str(step.get("title") or "")[:120])
            continue
        surviving_steps.append(step)

    if not surviving_steps and stripped_titles:
        # Locked: a procedure with zero remaining steps is dropped
        # entirely so the synthesizer doesn't get a phantom procedure.
        logger.info(
            "Procedure chunk %s dropped: all required steps gated by "
            "unconnected providers %s",
            chunk.id, stripped_provider_names,
        )
        return None

    meta["required_steps"] = surviving_steps
    meta["required_integrations"] = surviving_ints

    compliance = str(meta.get("compliance_level") or "should")
    if compliance not in {"must", "should", "may"}:
        compliance = "should"

    return RetrievedProcedure(
        chunk_id=chunk.id,
        doc_id=chunk.doc_id,
        doc_title=hit.doc_title,
        title=_extract_title(meta, chunk),
        content=chunk.text,
        score=hit.score,
        metadata=meta,
        compliance_level=compliance,
        stripped_step_titles=stripped_titles,
    )


def _build_reference(chunk: KBChunk, hit: SearchHit) -> RetrievedReference:
    meta = chunk.extracted_metadata if isinstance(chunk.extracted_metadata, dict) else {}
    return RetrievedReference(
        chunk_id=chunk.id,
        doc_id=chunk.doc_id,
        doc_title=hit.doc_title,
        kind=chunk.kind or "context",
        title=_extract_title(meta, chunk),
        content=chunk.text,
        score=hit.score,
        metadata=meta or {},
    )


def _extract_title(meta: Dict[str, Any], chunk: KBChunk) -> str:
    # The orchestrator may have synthesized a title; fall back to the
    # doc title via the SearchHit (already on the reference) or the
    # first line of the chunk text.
    if isinstance(meta, dict):
        title = meta.get("title")
        if title:
            return str(title)[:200]
    first_line = (chunk.text or "").split("\n", 1)[0]
    return first_line[:120]


__all__ = [
    "ActionPlanRetriever",
    "ActionPlanRetrievalResult",
    "RetrievedProcedure",
    "RetrievedReference",
]
