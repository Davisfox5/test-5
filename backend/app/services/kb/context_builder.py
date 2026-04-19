"""LINDA context-builder agent.

Reads the tenant's KB and produces a structured brief that LINDA (the
live-coaching + post-call analysis orchestrator) injects into its system
prompts. The brief is the compressed "voice of the company" that applies to
every call regardless of which customer is on the line — product details,
pricing rules, policies, tone of voice, and known objections.

Two operations:

* **Incremental merge** — on each KB upload/update, pass the existing brief
  plus the new document to Haiku. Cheap (<1.5s, ~$0.001 per call), preserves
  all prior content, and only grows as needed.
* **Full rebuild** — on demand (admin endpoint or startup). Reads every doc,
  batches them through Haiku in a scan-and-summarize pattern. Expected to be
  minutes-scale on large KBs.

Stored in ``Tenant.company_context`` JSONB so it's always available at call
time without a secondary fetch.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import KBDocument, Tenant

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_BRIEF_WORDS = 800
_MAX_DOC_CHARS = 12_000  # truncate very long docs when feeding into the merge

_BRIEF_SCHEMA_HINT = (
    "{\n"
    '  "company_overview": "1-3 sentence identity and what they sell",\n'
    '  "products_services": ["short bullets"],\n'
    '  "pricing_summary": "concise pricing rules, tiers, discounts",\n'
    '  "policies": ["refunds, SLAs, cancellation, compliance, etc."],\n'
    '  "tone_and_voice": "how the company wants agents to sound",\n'
    '  "key_differentiators": ["what sets them apart vs competitors"],\n'
    '  "known_objections": [{"objection": "...", "response": "..."}]\n'
    "}"
)

_SYSTEM_PROMPT = (
    "You are the context-builder agent for a conversation intelligence platform "
    "called LINDA. Your job is to maintain a living brief of the tenant's "
    "company knowledge, assembled from their knowledge-base documents.\n\n"
    "You will receive:\n"
    "1. The existing brief (JSON — may be empty).\n"
    "2. One or more KB documents that should be merged into it.\n\n"
    "Produce a new brief as JSON only (no prose, no code fences) matching this "
    f"schema:\n{_BRIEF_SCHEMA_HINT}\n\n"
    "Rules:\n"
    "- Preserve facts from the existing brief unless the new docs explicitly "
    "override them.\n"
    "- Deduplicate and consolidate — don't grow lists linearly.\n"
    f"- Keep the total under ~{_MAX_BRIEF_WORDS} words across all fields.\n"
    "- If a field has no information, use an empty string or empty array.\n"
    "- Never invent facts. If the docs don't mention tone, leave it blank.\n"
    "- Write in second-person imperative for tone_and_voice (e.g., 'Speak "
    "warmly but concisely')."
)


def _empty_brief() -> Dict:
    return {
        "company_overview": "",
        "products_services": [],
        "pricing_summary": "",
        "policies": [],
        "tone_and_voice": "",
        "key_differentiators": [],
        "known_objections": [],
    }


def _validate_brief(data: Dict) -> Dict:
    """Fill in missing keys with defaults. Tolerant — an LLM miss shouldn't
    wipe the brief."""
    out = _empty_brief()
    if not isinstance(data, dict):
        return out
    for key, default in out.items():
        value = data.get(key, default)
        if isinstance(default, list) and not isinstance(value, list):
            value = default
        elif isinstance(default, str) and not isinstance(value, str):
            value = str(value) if value is not None else ""
        out[key] = value
    return out


class ContextBuilderService:
    """Incremental and full-rebuild flows for the per-tenant brief."""

    def __init__(self, client: Optional[anthropic.AsyncAnthropic] = None) -> None:
        if client is not None:
            self._client = client
        else:
            settings = get_settings()
            self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def merge_document(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        doc: KBDocument,
    ) -> Dict:
        """Merge a single doc into the tenant's brief. Persists and returns it."""
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")
        existing = _validate_brief(tenant.company_context or {})
        updated = await self._call_haiku(existing, [doc])
        return await self._persist(db, tenant, updated, source_ids=[doc.id])

    async def rebuild_all(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> Dict:
        """Rebuild the brief from scratch by streaming every KB doc through
        the merge prompt. Tolerant of partial failures — if one batch errors,
        we keep the latest good brief and continue."""
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")

        stmt = (
            select(KBDocument)
            .where(KBDocument.tenant_id == tenant_id)
            .order_by(KBDocument.created_at.asc())
        )
        docs = list((await db.execute(stmt)).scalars().all())

        current = _empty_brief()
        processed_ids: List[uuid.UUID] = []

        # Batch of up to 4 docs per merge call to bound context size.
        BATCH = 4
        for i in range(0, len(docs), BATCH):
            batch = [d for d in docs[i : i + BATCH] if (d.content or "").strip()]
            if not batch:
                continue
            try:
                current = await self._call_haiku(current, batch)
                processed_ids.extend(d.id for d in batch)
            except Exception:
                logger.exception(
                    "Context rebuild batch %d..%d failed for tenant %s",
                    i,
                    i + len(batch),
                    tenant_id,
                )
                continue

        return await self._persist(db, tenant, current, source_ids=processed_ids)

    async def _call_haiku(
        self,
        existing: Dict,
        docs: List[KBDocument],
    ) -> Dict:
        doc_blocks = []
        for d in docs:
            body = (d.content or "").strip()
            if len(body) > _MAX_DOC_CHARS:
                body = body[:_MAX_DOC_CHARS] + "\n\n[truncated]"
            doc_blocks.append(
                f"### {d.title or 'Untitled'} (id={d.id})\n{body}"
            )

        user_message = (
            "## Existing brief (JSON)\n"
            f"{json.dumps(existing)}\n\n"
            "## New / updated knowledge-base documents\n"
            + "\n\n".join(doc_blocks)
            + "\n\nReturn the updated brief as JSON."
        )

        response = await self._client.messages.create(
            model=_MODEL,
            max_tokens=1600,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Context-builder returned non-JSON; keeping existing brief")
            return existing
        return _validate_brief(data)

    @staticmethod
    async def _persist(
        db: AsyncSession,
        tenant: Tenant,
        brief: Dict,
        source_ids: List[uuid.UUID],
    ) -> Dict:
        payload = dict(brief)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        payload["source_doc_ids"] = [str(sid) for sid in source_ids]
        tenant.company_context = payload
        return payload


def format_brief_for_prompt(brief: Dict) -> str:
    """Render the structured brief as a plain-text block for injection into
    LINDA's system prompts. Returns '' when the brief is effectively empty so
    we don't pollute the prompt with placeholder headers."""
    if not brief:
        return ""
    fields = [
        ("Overview", brief.get("company_overview", "")),
        ("Products / services", brief.get("products_services", [])),
        ("Pricing", brief.get("pricing_summary", "")),
        ("Policies", brief.get("policies", [])),
        ("Tone & voice", brief.get("tone_and_voice", "")),
        ("Key differentiators", brief.get("key_differentiators", [])),
    ]
    lines: List[str] = []
    for label, value in fields:
        if isinstance(value, list):
            if not value:
                continue
            lines.append(f"**{label}:**")
            for item in value:
                lines.append(f"- {item}")
        elif value:
            lines.append(f"**{label}:** {value}")

    objections = brief.get("known_objections") or []
    if objections:
        lines.append("**Known objections & responses:**")
        for obj in objections:
            if isinstance(obj, dict):
                q = obj.get("objection", "")
                a = obj.get("response", "")
                if q or a:
                    lines.append(f"- *{q}* → {a}")
            elif obj:
                lines.append(f"- {obj}")

    if not lines:
        return ""
    return "# Company context\n" + "\n".join(lines)
