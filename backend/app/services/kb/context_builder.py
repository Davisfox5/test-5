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

Stored in ``Tenant.tenant_context`` JSONB so it's always available at call
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
    '  "tenant_overview": "1-3 sentence identity and what they sell",\n'
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


# ── Brief shape ─────────────────────────────────────────────
#
# The tenant brief has three kinds of sections:
#
# 1. **KB-derived** (this file's ContextBuilderService owns these)
#    tenant_overview, products_services, pricing_summary, policies,
#    tone_and_voice, key_differentiators, known_objections.
#
# 2. **Onboarding/explicit** (owned by the tenant, set via
#    ``PUT /admin/tenant-context/fields``)
#    goals, kpis, strategies, org_structure, personal_touches.
#
# 3. **Learned** (the TenantBriefRefiner agent owns this)
#    playbook_insights.
#
# All three coexist in ``Tenant.tenant_context``. Each agent preserves
# sections it doesn't own so they survive across runs.


_ONBOARDING_KEYS = {"goals", "kpis", "strategies", "org_structure", "personal_touches"}
_LEARNED_KEYS = {"playbook_insights"}


def _empty_personal_touches() -> Dict:
    return {
        "greeting_style": "",
        "signoff_style": "",
        "phrasing_preferences": [],  # [{"say": str, "dont_say": str, "context": str}]
        "rituals": [],               # ["Handwritten note on closed-won >$10k", ...]
        "humor_level": "",           # formal | warm | playful | professional
        "pacing_style": "",          # match_caller | deliberate | energetic
        "empathy_markers": [],
        "celebration_markers": [],
        "avoid_phrases": [],
        "signature_tagline": "",
    }


def _empty_playbook_insights() -> Dict:
    return {
        "what_works": [],               # ["Opening with ROI line", ...]
        "what_doesnt": [],
        "top_performing_phrases": [],
        "common_failure_modes": [],
        "winning_objection_handlers": [],  # [{"objection": str, "handler": str}]
        "last_learned_at": "",
        "sample_size": 0,
    }


def _empty_brief() -> Dict:
    return {
        # KB-derived
        "tenant_overview": "",
        "products_services": [],
        "pricing_summary": "",
        "policies": [],
        "tone_and_voice": "",
        "key_differentiators": [],
        "known_objections": [],
        # Onboarding / explicit
        "goals": [],
        "kpis": [],               # [{"name": str, "target": number|str, "current": number|null}]
        "strategies": [],
        "org_structure": {},      # {"teams": [...], "escalation_path": [...], "territories": [...]}
        "personal_touches": _empty_personal_touches(),
        # Learned from outcomes
        "playbook_insights": _empty_playbook_insights(),
    }


def _coerce(value, default):
    """Shallow coerce ``value`` into the shape of ``default``."""
    if isinstance(default, list):
        return list(value) if isinstance(value, list) else default
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return dict(default)
        # Merge top-level keys so unknown extras on the LLM side don't crash,
        # and missing keys come from defaults.
        merged = dict(default)
        for k, v in value.items():
            merged[k] = v
        return merged
    if isinstance(default, str):
        return str(value) if value is not None and not isinstance(value, (list, dict)) else default
    if isinstance(default, (int, float)):
        try:
            return type(default)(value)
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


def _validate_brief(data: Dict) -> Dict:
    """Fill in missing keys with defaults. Tolerant — an LLM miss shouldn't
    wipe the brief."""
    out = _empty_brief()
    if not isinstance(data, dict):
        return out
    for key, default in out.items():
        out[key] = _coerce(data.get(key, default), default)
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
        existing = _validate_brief(tenant.tenant_context or {})
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

        # Start from the existing brief so we preserve onboarding + learned
        # sections across a full KB rebuild. The merger only overwrites the
        # KB-derived subset.
        current = _validate_brief(tenant.tenant_context or {})
        # Wipe the KB-derived subset so old facts from deleted docs don't
        # linger — the merger will rebuild those from the docs we feed in.
        for key in list(_empty_brief().keys()):
            if key in _ONBOARDING_KEYS or key in _LEARNED_KEYS:
                continue
            current[key] = _empty_brief()[key]
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
        # Only expose the KB-derived subset to the merger so it doesn't
        # rewrite onboarding or learned sections. We merge those back in
        # after Haiku returns.
        existing = _validate_brief(existing)
        kb_subset = {
            k: v
            for k, v in existing.items()
            if k not in _ONBOARDING_KEYS and k not in _LEARNED_KEYS
        }

        doc_blocks = []
        for d in docs:
            body = (d.content or "").strip()
            if len(body) > _MAX_DOC_CHARS:
                body = body[:_MAX_DOC_CHARS] + "\n\n[truncated]"
            doc_blocks.append(
                f"### {d.title or 'Untitled'} (id={d.id})\n{body}"
            )

        user_message = (
            "## Existing brief (JSON — KB-derived sections only)\n"
            f"{json.dumps(kb_subset)}\n\n"
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

        # Splice Haiku's KB-derived updates back into the full brief, keeping
        # onboarding + learned sections untouched.
        merged = dict(existing)
        for k, v in data.items():
            if k in _ONBOARDING_KEYS or k in _LEARNED_KEYS:
                continue  # Haiku should not be writing these; ignore if it did
            merged[k] = v
        return _validate_brief(merged)

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
        tenant.tenant_context = payload
        return payload


def _render_list(label: str, values) -> List[str]:
    if not values:
        return []
    out = [f"**{label}:**"]
    for item in values:
        out.append(f"- {item}")
    return out


def _render_personal_touches(pt: Dict) -> List[str]:
    if not isinstance(pt, dict):
        return []
    out: List[str] = []
    simple_fields = [
        ("Greeting style", pt.get("greeting_style", "")),
        ("Sign-off style", pt.get("signoff_style", "")),
        ("Humor level", pt.get("humor_level", "")),
        ("Pacing style", pt.get("pacing_style", "")),
        ("Signature tagline", pt.get("signature_tagline", "")),
    ]
    for label, value in simple_fields:
        if value:
            out.append(f"- {label}: {value}")

    phrasing = pt.get("phrasing_preferences") or []
    if phrasing:
        out.append("- Preferred phrasing:")
        for p in phrasing:
            if isinstance(p, dict):
                say = p.get("say", "")
                dont = p.get("dont_say", "")
                if say or dont:
                    out.append(f"  - say \"{say}\", not \"{dont}\"")

    for label, key in [
        ("Rituals", "rituals"),
        ("Empathy markers", "empathy_markers"),
        ("Celebration markers", "celebration_markers"),
        ("Phrases to avoid", "avoid_phrases"),
    ]:
        vals = pt.get(key) or []
        if vals:
            out.append(f"- {label}: {', '.join(str(v) for v in vals)}")

    if out:
        out.insert(0, "**Personal touches:**")
    return out


def _render_playbook(pb: Dict) -> List[str]:
    if not isinstance(pb, dict):
        return []
    out: List[str] = []
    for label, key in [
        ("What's working recently", "what_works"),
        ("What's not working", "what_doesnt"),
        ("Top phrases", "top_performing_phrases"),
        ("Common failure modes", "common_failure_modes"),
    ]:
        vals = pb.get(key) or []
        if vals:
            out.append(f"- {label}: {', '.join(str(v) for v in vals)}")

    handlers = pb.get("winning_objection_handlers") or []
    if handlers:
        out.append("- Winning objection handlers:")
        for h in handlers:
            if isinstance(h, dict):
                obj = h.get("objection", "")
                ans = h.get("handler", "")
                if obj or ans:
                    out.append(f"  - *{obj}* → {ans}")

    if out:
        out.insert(0, "**Playbook insights (learned from outcomes):**")
    return out


def format_brief_for_prompt(brief: Dict) -> str:
    """Render the structured brief as a plain-text block for injection into
    LINDA's system prompts. Returns '' when the brief is effectively empty so
    we don't pollute the prompt with placeholder headers."""
    if not brief:
        return ""

    lines: List[str] = []

    # KB-derived sections.
    overview = brief.get("tenant_overview", "")
    if overview:
        lines.append(f"**Overview:** {overview}")
    lines += _render_list("Products / services", brief.get("products_services", []))
    pricing = brief.get("pricing_summary", "")
    if pricing:
        lines.append(f"**Pricing:** {pricing}")
    lines += _render_list("Policies", brief.get("policies", []))
    tone = brief.get("tone_and_voice", "")
    if tone:
        lines.append(f"**Tone & voice:** {tone}")
    lines += _render_list("Key differentiators", brief.get("key_differentiators", []))

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

    # Onboarding / explicit sections.
    lines += _render_list("Goals", brief.get("goals", []))
    kpis = brief.get("kpis") or []
    if kpis:
        lines.append("**KPIs:**")
        for k in kpis:
            if isinstance(k, dict):
                name = k.get("name", "")
                target = k.get("target", "")
                current = k.get("current")
                suffix = f" (current: {current})" if current is not None else ""
                lines.append(f"- {name}: target {target}{suffix}")
            else:
                lines.append(f"- {k}")
    lines += _render_list("Strategies", brief.get("strategies", []))

    org = brief.get("org_structure") or {}
    if isinstance(org, dict):
        teams = org.get("teams") or []
        esc = org.get("escalation_path") or []
        terr = org.get("territories") or []
        if any([teams, esc, terr]):
            lines.append("**Org structure:**")
            if teams:
                lines.append(f"- Teams: {', '.join(str(t) for t in teams)}")
            if esc:
                lines.append(f"- Escalation: {' → '.join(str(e) for e in esc)}")
            if terr:
                lines.append(f"- Territories: {', '.join(str(t) for t in terr)}")

    lines += _render_personal_touches(brief.get("personal_touches") or {})
    lines += _render_playbook(brief.get("playbook_insights") or {})

    if not lines:
        return ""
    return "# Tenant context\n" + "\n".join(lines)
