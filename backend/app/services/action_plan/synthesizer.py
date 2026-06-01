"""Action Plan Synthesizer - composes the plan via Calls A, B, and C.

Pipeline:

1. **Resolve the active domain** for this call. Per-team default takes
   precedence; triage's domain_prediction overrides only when
   confidence >= 0.8 (locked).
2. **Gather context**: procedures + reference chunks (kind-filtered +
   integration-gated), customer brief external context (CRM fan-out
   with <15min cache), tenant capabilities block.
3. **Call A** - candidate generation. Haiku by default; Sonnet if the
   triage complexity is high. Prompt-cached system prompt.
4. **Call B** - composition + KB compliance check. Always Sonnet -
   this is the reasoning step.
5. **Call C** - per-step artifact rendering. Per the cost-saving
   decision: Sonnet for the customer_endpoint step, Haiku for
   everything else. Issued concurrently across non-endpoint steps.
6. **Persist** the resulting ActionPlan + ActionStep rows + first
   StepArtifact version per step.

Failure modes (locked decisions):

* Malformed JSON in Call A/B: retry once on Sonnet with a stricter
  prompt. If that still fails, raise so the caller can fall back to
  the manual-creation path.
* CRM fetch failure: use stale cache if present; otherwise proceed
  without CRM data. The plan still synthesizes.
* Procedure forces a step that doesn't fit: included verbatim, no
  soft warning (the strictest reading of "procedures always win").
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionPlan,
    ActionStep,
    Interaction,
    StepArtifact,
    Tenant,
    User,
)
from backend.app.services.action_plan.domains import (
    REGISTRY as DOMAIN_REGISTRY,
    DomainTemplate,
    get as get_domain,
)
from backend.app.services.action_plan.external_context import (
    ExternalContextResult,
    build_capabilities_block,
    fetch_external_context,
)
from backend.app.services.action_plan.prompts import (
    ACTION_PLAN_PROMPT_VERSION,
    CALL_A_SYSTEM_PROMPT,
    CALL_B_SYSTEM_PROMPT,
    CALL_C_PAYLOAD_SCHEMAS,
    CALL_C_SYSTEM_PROMPT,
)
from backend.app.services.kb.action_plan_retrieve import (
    ActionPlanRetriever,
    ActionPlanRetrievalResult,
    RetrievedProcedure,
    RetrievedReference,
)
from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.triage_service import (
    DOMAIN_OVERRIDE_CONFIDENCE_THRESHOLD,
)

logger = logging.getLogger(__name__)


_MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


# ──────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────


@dataclass
class SynthesisInputs:
    """Everything the synthesizer needs to compose a plan."""

    tenant: Tenant
    interaction: Interaction
    transcript_text: str
    triage: Dict[str, Any]
    # Optional override; when None, resolves from triage + team default.
    forced_domain: Optional[str] = None
    # Pre-computed by the orchestrator pipeline. When None, the
    # synthesizer derives a quick summary from triage to feed retrieval.
    customer_id: Optional[uuid.UUID] = None
    # Optional acting user — drives per-user default_domain.
    acting_user_id: Optional[uuid.UUID] = None


@dataclass
class SynthesisResult:
    plan_id: uuid.UUID
    plan: ActionPlan
    steps: List[ActionStep]
    retrieval: ActionPlanRetrievalResult
    external_context: ExternalContextResult
    chosen_domain: str
    domain_source: str  # 'forced' | 'triage_override' | 'team_default' | 'tenant_default'


# ──────────────────────────────────────────────────────────
# Domain resolution
# ──────────────────────────────────────────────────────────


async def resolve_domain(
    db: AsyncSession,
    *,
    tenant: Tenant,
    acting_user_id: Optional[uuid.UUID],
    triage: Dict[str, Any],
    forced_domain: Optional[str],
) -> Tuple[str, str]:
    """Return (domain_name, source_label).

    Order: forced override > triage prediction (>= 0.8 confidence) >
    per-user default > tenant default.
    """
    if forced_domain and forced_domain in DOMAIN_REGISTRY:
        return forced_domain, "forced"

    prediction = triage.get("domain_prediction") or {}
    predicted_domain = prediction.get("domain")
    try:
        predicted_conf = float(prediction.get("confidence") or 0.0)
    except (TypeError, ValueError):
        predicted_conf = 0.0
    if (
        predicted_domain in DOMAIN_REGISTRY
        and predicted_conf >= DOMAIN_OVERRIDE_CONFIDENCE_THRESHOLD
    ):
        return predicted_domain, "triage_override"

    if acting_user_id is not None:
        user = await db.get(User, acting_user_id)
        if user is not None and user.default_domain in DOMAIN_REGISTRY:
            return user.default_domain, "team_default"

    if tenant.default_domain in DOMAIN_REGISTRY:
        return tenant.default_domain, "tenant_default"
    return "generic", "tenant_default"


# ──────────────────────────────────────────────────────────
# Prompt block renderers (text-only - no JSON parsing here)
# ──────────────────────────────────────────────────────────


def _render_procedures_block(procedures: List[RetrievedProcedure]) -> str:
    if not procedures:
        return "(no procedures matched this call)"
    lines: List[str] = []
    for i, p in enumerate(procedures, start=1):
        meta = p.metadata
        triggers = ", ".join(meta.get("triggers") or []) or "(no triggers)"
        applies = meta.get("applies_when") or ""
        steps = meta.get("required_steps") or []
        ints = meta.get("required_integrations") or []
        head = f"[P{i}] {p.title or '(untitled procedure)'} - compliance={p.compliance_level}"
        lines.append(head)
        if applies:
            lines.append(f"  applies_when: {applies}")
        lines.append(f"  triggers: {triggers}")
        lines.append(
            f"  citation: doc_id={p.doc_id} chunk_id={p.chunk_id}"
        )
        if steps:
            lines.append("  required_steps:")
            for j, s in enumerate(steps, start=1):
                title = s.get("title") if isinstance(s, dict) else str(s)
                desc = s.get("description") if isinstance(s, dict) else ""
                lines.append(f"    {j}. {title}")
                if desc:
                    lines.append(f"       {desc}")
        if ints:
            int_summary = ", ".join(
                f"{i.get('provider')}.{i.get('operation')}"
                for i in ints if isinstance(i, dict)
            )
            lines.append(f"  required_integrations: {int_summary}")
    return "\n".join(lines)


def _render_procedures_summary_block(
    procedures: List[RetrievedProcedure],
) -> str:
    """Compact summary used in Call B's compliance check."""
    if not procedures:
        return "(no procedures matched this call)"
    lines: List[str] = []
    for i, p in enumerate(procedures, start=1):
        steps = p.metadata.get("required_steps") or []
        step_titles = [
            s.get("title") if isinstance(s, dict) else str(s)
            for s in steps
        ]
        lines.append(
            f"[P{i}] {p.title} (compliance={p.compliance_level}; "
            f"chunk_id={p.chunk_id}): "
            f"{'; '.join(t for t in step_titles if t)}"
        )
    return "\n".join(lines)


def _render_articles_block(references: List[RetrievedReference]) -> str:
    if not references:
        return "(no reference articles matched)"
    lines: List[str] = []
    for r in references:
        lines.append(f"[{r.kind}] {r.title or '(untitled)'}")
        snippet = (r.content or "")[:600]
        lines.append(f"  {snippet}")
    return "\n".join(lines)


def _render_candidates_block(candidates: List[Dict[str, Any]]) -> str:
    if not candidates:
        return "(no candidates emitted by Call A)"
    lines: List[str] = []
    for i, c in enumerate(candidates):
        kb = c.get("kb_source")
        kb_str = (
            f" [grounded in chunk {kb.get('chunk_id')}]"
            if isinstance(kb, dict) and kb.get("chunk_id")
            else ""
        )
        lines.append(
            f"[{i}] {c.get('title', '(untitled)')} "
            f"(channel={c.get('channel')}, urgency={c.get('urgency')})"
            f"{kb_str}"
        )
        if c.get("intent"):
            lines.append(f"    intent: {c['intent']}")
        out = c.get("output_schema") or []
        if out:
            slot_keys = ", ".join(
                s.get("slot_key") if isinstance(s, dict) else str(s)
                for s in out
            )
            lines.append(f"    produces: {slot_keys}")
    return "\n".join(lines)


def _render_loop_in_examples(template: DomainTemplate) -> str:
    if not template.loop_in_role_examples:
        return "(none)"
    return ", ".join(template.loop_in_role_examples)


def _render_output_slot_examples(template: DomainTemplate) -> str:
    if not template.output_slot_examples:
        return "  (none)"
    return "\n".join(
        f"  - {ex.slot_key}: {ex.description}"
        for ex in template.output_slot_examples
    )


def _render_goal_examples(template: DomainTemplate) -> str:
    if not template.goal_examples:
        return "(none)"
    return ", ".join(f"\"{g}\"" for g in template.goal_examples)


# ──────────────────────────────────────────────────────────
# Synthesizer
# ──────────────────────────────────────────────────────────


class ActionPlanSynthesizer:
    """End-to-end plan composition.

    One ``synthesize`` call: takes a SynthesisInputs, returns a
    persisted ActionPlan + steps + first-version artifacts.
    """

    def __init__(
        self,
        client: Optional[anthropic.AsyncAnthropic] = None,
        retriever: Optional[ActionPlanRetriever] = None,
    ) -> None:
        self._client = client or get_async_anthropic()
        self._retriever = retriever or ActionPlanRetriever()

    async def synthesize(
        self,
        db: AsyncSession,
        inputs: SynthesisInputs,
    ) -> SynthesisResult:
        # action_plans.interaction_id has a UNIQUE constraint (one plan
        # per interaction). On redrive we want the latest analysis to
        # drive the plan, so delete any existing plan for this
        # interaction before composing a fresh one. ``cascade="all,
        # delete-orphan"`` on the ActionPlan→ActionStep relationship
        # handles step rows. Manually-created plans (manually_created=
        # True) are NOT auto-replaced — those represent rep-authored
        # follow-ups that shouldn't get blown away by a re-run of AI
        # analysis. Without this guard every redrive after the first
        # one silently fails with a UniqueViolationError, the outer
        # try/except in tasks.py swallows it, and the user sees no
        # plan refresh despite a successful redrive.
        from sqlalchemy import delete as _sql_delete
        await db.execute(
            _sql_delete(ActionPlan).where(
                ActionPlan.interaction_id == inputs.interaction.id,
                ActionPlan.tenant_id == inputs.tenant.id,
                ActionPlan.manually_created == False,  # noqa: E712
            )
        )
        await db.flush()

        domain, domain_source = await resolve_domain(
            db,
            tenant=inputs.tenant,
            acting_user_id=inputs.acting_user_id,
            triage=inputs.triage,
            forced_domain=inputs.forced_domain,
        )
        template = get_domain(domain)

        # Retrieve KB and CRM context concurrently.
        query = _retrieval_query(inputs)
        retrieval = await self._retriever.retrieve(
            db,
            tenant_id=inputs.tenant.id,
            query=query,
            domain=domain,
        )
        external = await fetch_external_context(
            db,
            tenant_id=inputs.tenant.id,
            customer_id=inputs.customer_id,
        )

        capabilities_block = build_capabilities_block(
            external.connected_providers
            or retrieval.connected_providers
        )

        # Call A: candidates
        candidates = await self._call_a(
            template=template,
            tenant=inputs.tenant,
            transcript_text=inputs.transcript_text,
            triage=inputs.triage,
            retrieval=retrieval,
            external=external,
            capabilities_block=capabilities_block,
        )

        # Empty candidates is a legitimate outcome (call fully
        # resolved + no procedure mandates post-call work). Persist an
        # empty plan with status='completed' so the UI shows
        # "no follow-up needed" rather than a synthesis failure.
        if not candidates:
            plan, steps = await self._persist_empty_plan(
                db=db,
                inputs=inputs,
                domain=domain,
                external=external,
                retrieval=retrieval,
            )
            return SynthesisResult(
                plan_id=plan.id,
                plan=plan,
                steps=steps,
                retrieval=retrieval,
                external_context=external,
                chosen_domain=domain,
                domain_source=domain_source,
            )

        # Call B: composition
        composition = await self._call_b(
            template=template,
            tenant=inputs.tenant,
            candidates=candidates,
            retrieval=retrieval,
        )

        # Persist plan + steps before rendering artifacts so each step
        # has an id that Call C can attach an artifact to.
        plan, steps, index_to_step_id = await self._persist_plan(
            db=db,
            inputs=inputs,
            domain=domain,
            composition=composition,
            external=external,
            retrieval=retrieval,
        )

        # Call C: artifact rendering per step. The customer endpoint
        # gets Sonnet; everything else gets Haiku (cost decision).
        await self._render_artifacts(
            db=db,
            tenant=inputs.tenant,
            template=template,
            steps=steps,
            composition=composition,
            external=external,
            retrieval=retrieval,
        )

        return SynthesisResult(
            plan_id=plan.id,
            plan=plan,
            steps=steps,
            retrieval=retrieval,
            external_context=external,
            chosen_domain=domain,
            domain_source=domain_source,
        )

    # ── Call A ────────────────────────────────────────────

    async def _call_a(
        self,
        *,
        template: DomainTemplate,
        tenant: Tenant,
        transcript_text: str,
        triage: Dict[str, Any],
        retrieval: ActionPlanRetrievalResult,
        external: ExternalContextResult,
        capabilities_block: str,
    ) -> List[Dict[str, Any]]:
        system_prompt = CALL_A_SYSTEM_PROMPT.format(
            domain_role=template.role,
            tenant_name=tenant.name,
            procedures_block=_render_procedures_block(retrieval.procedures),
            articles_block=_render_articles_block(retrieval.references),
            customer_brief_block=external.to_brief_block(),
            tenant_capabilities_block=capabilities_block,
            loop_in_role_examples=_render_loop_in_examples(template),
            output_slot_examples=_render_output_slot_examples(template),
        )
        user_content = _format_transcript_for_call(
            transcript_text, triage, max_chars=12_000,
        )

        tier = (triage.get("recommended_tier") or "haiku").lower()
        data = await self._call_with_retry(
            system_prompt=system_prompt,
            user_content=user_content,
            primary_tier=tier,
            max_tokens=4096,
            label="action_plan.call_a",
        )
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            return []
        return candidates[:15]  # hard cap from prompt

    # ── Call B ────────────────────────────────────────────

    async def _call_b(
        self,
        *,
        template: DomainTemplate,
        tenant: Tenant,
        candidates: List[Dict[str, Any]],
        retrieval: ActionPlanRetrievalResult,
    ) -> Dict[str, Any]:
        system_prompt = CALL_B_SYSTEM_PROMPT.format(
            domain_role=template.role,
            customer_endpoint_archetype=template.customer_endpoint_archetype,
            customer_endpoint_description=template.customer_endpoint_description,
            goal_examples=_render_goal_examples(template),
            procedures_summary_block=_render_procedures_summary_block(
                retrieval.procedures
            ),
            candidates_block=_render_candidates_block(candidates),
        )
        user_content = (
            "Compose the plan now. Cluster, order, identify the customer "
            "endpoint, wire input slots, verify procedure compliance, and "
            "return the JSON per the schema in the system prompt.\n\n"
            "Raw candidates (for cross-reference):\n"
            + json.dumps(candidates, indent=2)
        )

        # Call B is always Sonnet - this is the reasoning step.
        data = await self._call_with_retry(
            system_prompt=system_prompt,
            user_content=user_content,
            primary_tier="sonnet",
            max_tokens=6000,
            label="action_plan.call_b",
        )
        if not isinstance(data, dict) or "steps" not in data:
            raise SynthesisFailedError("Call B returned no usable plan")
        return data

    # ── Call C ────────────────────────────────────────────

    async def _render_artifacts(
        self,
        *,
        db: AsyncSession,
        tenant: Tenant,
        template: DomainTemplate,
        steps: List[ActionStep],
        composition: Dict[str, Any],
        external: ExternalContextResult,
        retrieval: ActionPlanRetrievalResult,
    ) -> None:
        """Render the first artifact for each step.

        Per cost decision: Sonnet for customer_endpoint; Haiku for the
        rest. Each step renders independently so we can issue them in
        parallel without dependency entanglement.
        """
        # Per-channel template chunk lookup, by step channel name.
        template_chunks_by_channel: Dict[str, RetrievedReference] = {}
        for ref in retrieval.references:
            if ref.kind == "template":
                # Use the template's applies_to as a coarse channel match.
                applies = (
                    ref.metadata.get("applies_to")
                    if isinstance(ref.metadata, dict)
                    else ""
                ) or ""
                key = str(applies).lower()
                template_chunks_by_channel.setdefault(key, ref)

        # Build the per-channel job set.
        for step in steps:
            channel = (step.recommended_channel or "note").lower()
            schema = CALL_C_PAYLOAD_SCHEMAS.get(channel)
            if schema is None:
                # Unknown channel - fall back to a free-form note.
                channel = "note"
                schema = CALL_C_PAYLOAD_SCHEMAS["note"]

            is_endpoint = step.role_in_plan == "customer_endpoint"
            tier = "sonnet" if is_endpoint else "haiku"

            template_chunk = template_chunks_by_channel.get(channel)
            kb_template_block = (
                f"{template_chunk.title}\n{template_chunk.content}"
                if template_chunk is not None
                else "(no template in KB)"
            )

            system_prompt = CALL_C_SYSTEM_PROMPT.format(
                domain_role=template.role,
                tone=template.tone,
                tone_description=template.tone_description,
                tenant_name=tenant.name,
                summary_block=_summary_block_for_artifact(composition),
                customer_brief_block=external.to_brief_block(),
                step_title=step.title,
                step_intent=step.intent or step.description or "",
                step_channel=channel,
                step_participants=_format_participants(step.participants),
                filled_slots_block=_format_filled_slots(step.input_slots),
                output_schema_block=_format_output_schema(step.output_schema),
                kb_template_block=kb_template_block,
                payload_schema_block=schema,
            )
            user_content = (
                f"Draft the {channel} artifact now. Return ONLY the JSON "
                "per the schema in the system prompt."
            )

            try:
                payload = await self._call_with_retry(
                    system_prompt=system_prompt,
                    user_content=user_content,
                    primary_tier=tier,
                    max_tokens=2500 if not is_endpoint else 4000,
                    label="action_plan.call_c",
                )
            except SynthesisFailedError:
                logger.warning(
                    "Call C failed for step %s (%s); leaving artifact empty",
                    step.id, channel,
                )
                payload = {}

            if not isinstance(payload, dict):
                payload = {}

            new_version = (step.artifact_version or 0) + 1
            artifact = StepArtifact(
                step_id=step.id,
                tenant_id=tenant.id,
                version=new_version,
                kind=_artifact_kind_for_channel(channel),
                payload=payload,
                model_tier=tier,
            )
            db.add(artifact)
            step.artifact_version = new_version
            step.artifact_stale = False

    # ── Persistence ───────────────────────────────────────

    async def _persist_plan(
        self,
        *,
        db: AsyncSession,
        inputs: SynthesisInputs,
        domain: str,
        composition: Dict[str, Any],
        external: ExternalContextResult,
        retrieval: ActionPlanRetrievalResult,
    ) -> Tuple[ActionPlan, List[ActionStep], Dict[int, uuid.UUID]]:
        plan = ActionPlan(
            tenant_id=inputs.tenant.id,
            interaction_id=inputs.interaction.id,
            customer_id=inputs.customer_id,
            goal=str(composition.get("goal") or "")[:200] or None,
            domain=domain,
            status="active",
            procedures_applied=[
                {
                    "doc_id": str(p.doc_id),
                    "chunk_id": str(p.chunk_id),
                    "title": p.title,
                    "compliance_level": p.compliance_level,
                }
                for p in retrieval.procedures
            ],
            external_context_snapshot=_snapshot_external_context(external),
            manually_created=False,
        )
        db.add(plan)
        await db.flush()

        raw_steps = composition.get("steps") or []
        if not isinstance(raw_steps, list):
            raw_steps = []

        # Two-pass: first create all step rows so depends_on indices can
        # be translated to step IDs.
        index_to_step_id: Dict[int, uuid.UUID] = {}
        step_rows: List[ActionStep] = []
        for idx, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                continue
            role = str(s.get("role_in_plan") or "preparation")
            if role not in {"preparation", "customer_endpoint", "post_completion"}:
                role = "preparation"
            compliance = s.get("compliance_level")
            if compliance not in {"must", "should", "may", None}:
                compliance = None
            kb_source = s.get("kb_source") if isinstance(s.get("kb_source"), dict) else None
            channel = str(s.get("channel") or s.get("recommended_channel") or "note")
            # awaits_response: emitted by Call B. Coerce + default to
            # False so legacy plans (and any malformed payload) don't
            # accidentally hold dependent steps in awaiting_response.
            _awaits = s.get("awaits_response")
            if not isinstance(_awaits, bool):
                _awaits = False
            step = ActionStep(
                plan_id=plan.id,
                tenant_id=inputs.tenant.id,
                title=str(s.get("title") or "Untitled step")[:255],
                description=s.get("description"),
                intent=s.get("intent"),
                priority=str(s.get("priority") or "medium"),
                recommended_channel=channel,
                channel_reasoning=s.get("channel_reasoning"),
                participants=s.get("participants") or [],
                prep_artifacts=s.get("prep_needed") or s.get("prep_artifacts") or [],
                implicit_signal=s.get("implicit_signal"),
                state="ready",  # readiness recomputed once depends_on lands
                output_schema=s.get("output_schema") or [],
                input_slots=_normalize_input_slots(s.get("input_slots")),
                kb_source=kb_source,
                compliance_level=compliance,
                role_in_plan=role,
                target_integration=s.get("target_integration"),
                integration_operation=s.get("integration_operation"),
                awaits_response=_awaits,
            )
            db.add(step)
            await db.flush()
            index_to_step_id[idx] = step.id
            step_rows.append(step)

        # Second pass: translate depends_on indices to ids and resolve
        # input_slots.filled_by_step_index -> filled_by_step_id.
        for idx, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                continue
            step = step_rows[idx] if idx < len(step_rows) else None
            if step is None:
                continue
            depends_indices = s.get("depends_on") or []
            if not isinstance(depends_indices, list):
                depends_indices = []
            step.depends_on = [
                str(index_to_step_id[i])
                for i in depends_indices
                if isinstance(i, int) and i in index_to_step_id
            ]
            # Resolve input_slots.filled_by_step_index -> id
            new_slots = []
            for slot in step.input_slots or []:
                if not isinstance(slot, dict):
                    continue
                fb_idx = slot.get("filled_by_step_index")
                fb_id = (
                    str(index_to_step_id[fb_idx])
                    if isinstance(fb_idx, int) and fb_idx in index_to_step_id
                    else None
                )
                new_slots.append(
                    {
                        "slot_key": str(slot.get("slot_key") or ""),
                        "description": str(slot.get("description") or ""),
                        "required": bool(slot.get("required", True)),
                        "filled_by_step_id": fb_id,
                        "filled_value": None,
                        "filled_at": None,
                    }
                )
            step.input_slots = new_slots
            # Initial readiness: blocked if any dependency hasn't completed.
            if step.depends_on:
                step.state = "blocked"

        # Customer endpoint pointer
        endpoint_idx = composition.get("customer_endpoint_index")
        if isinstance(endpoint_idx, int) and endpoint_idx in index_to_step_id:
            plan.customer_endpoint_step_id = index_to_step_id[endpoint_idx]

        # Seed obvious slots from the source interaction so the rep
        # doesn't see ``{contact_name}`` / ``{call_date}`` etc. literal
        # placeholders in artifact bodies. The slot system was always
        # designed to fill from upstream steps OR call data; this pass
        # handles the call-data side, which was previously unwired.
        try:
            seeded = await _seed_slots_from_interaction(
                db,
                steps=step_rows,
                interaction=inputs.interaction,
            )
            if seeded:
                logger.info(
                    "seeded %d slots from interaction for plan %s",
                    seeded, plan.id,
                )
        except Exception:  # noqa: BLE001 — slot-seed must never fail synthesis
            logger.exception(
                "slot seeding failed for plan %s (non-fatal)", plan.id,
            )

        await db.flush()
        return plan, step_rows, index_to_step_id

    async def _persist_empty_plan(
        self,
        *,
        db: AsyncSession,
        inputs: SynthesisInputs,
        domain: str,
        external: ExternalContextResult,
        retrieval: ActionPlanRetrievalResult,
    ) -> Tuple[ActionPlan, List[ActionStep]]:
        plan = ActionPlan(
            tenant_id=inputs.tenant.id,
            interaction_id=inputs.interaction.id,
            customer_id=inputs.customer_id,
            goal="No follow-up required",
            domain=domain,
            status="completed",
            external_context_snapshot=_snapshot_external_context(external),
            procedures_applied=[],
            completed_at=datetime.now(timezone.utc),
            manually_created=False,
        )
        db.add(plan)
        await db.flush()
        return plan, []

    # ── LLM call helper (with retry + Sonnet upgrade on bad JSON) ──

    async def _call_with_retry(
        self,
        *,
        system_prompt: str,
        user_content: str,
        primary_tier: str,
        max_tokens: int,
        label: str,
    ) -> Dict[str, Any]:
        """Single LLM call with one retry per the failure-mode decision.

        Retry: upgrade Haiku to Sonnet (or stay on Sonnet if already
        there) AND append a stricter "return ONLY valid JSON" reminder.
        After the retry, raise SynthesisFailedError so the caller can
        surface the failure to the user.
        """
        attempts = [
            (primary_tier, system_prompt),
            (
                "sonnet" if primary_tier != "sonnet" else "sonnet",
                system_prompt + "\n\nREMINDER: Return ONLY valid JSON. "
                "No markdown fences, no commentary, no leading or "
                "trailing text. If the previous attempt failed to parse, "
                "this attempt must succeed.",
            ),
        ]
        last_error: Optional[str] = None
        for attempt_idx, (tier, prompt) in enumerate(attempts):
            try:
                response = await self._client.messages.create(
                    model=_MODELS.get(tier, _MODELS["sonnet"]),
                    max_tokens=max_tokens,
                    system=[
                        {
                            "type": "text",
                            "text": prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[
                        {"role": "user", "content": user_content}
                    ],
                )
                raw_text = response.content[0].text
                from backend.app.services.triage_service import _strip_json_fences
                data = json.loads(_strip_json_fences(raw_text))
                return data
            except (
                anthropic.APIError,
                json.JSONDecodeError,
                IndexError,
                KeyError,
                AttributeError,
            ) as exc:
                last_error = str(exc)
                logger.warning(
                    "%s attempt %d (%s) failed: %s",
                    label, attempt_idx + 1, tier, exc,
                )
                continue
        raise SynthesisFailedError(
            f"{label} failed after {len(attempts)} attempts: {last_error}"
        )


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────


class SynthesisFailedError(RuntimeError):
    """Raised when the LLM calls failed and we can't recover.

    The caller (the Celery pipeline task) catches this and falls back
    to no-plan + a visible message in the UI per the locked failure-
    mode decision.
    """


def _retrieval_query(inputs: SynthesisInputs) -> str:
    """Build the retrieval query from triage + a transcript head sample.

    We deliberately favour the triage quick_summary + topics. These
    are a dense, well-structured signal that lands better in vector
    search than raw transcript text.

    Every part is force-stringified before the join so a caller that
    accidentally hands us a list-of-dicts (which the pipeline did:
    ``compressed_for_llm`` is a list of segment dicts, not text)
    can't crash synthesis with a cryptic TypeError. The fallback for
    a non-string transcript_text is to JSON-stringify it and trim;
    cheap to compute and adequate for vector retrieval seeding.
    """
    import json as _json

    parts: List[str] = []
    summary = inputs.triage.get("quick_summary")
    if summary:
        parts.append(str(summary))
    topics = inputs.triage.get("topics") or []
    if topics:
        parts.append("Topics: " + ", ".join(str(t) for t in topics))
    txt = inputs.transcript_text
    if txt:
        if isinstance(txt, str):
            parts.append(txt[:800])
        elif isinstance(txt, list):
            # Segment-dict list from the worker pipeline. Flatten each
            # segment's ``text`` field; fall back to a JSON serialization
            # for anything that doesn't look like a segment.
            flattened: List[str] = []
            for seg in txt[:50]:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    flattened.append(seg["text"])
                else:
                    flattened.append(_json.dumps(seg, default=str))
            parts.append(" ".join(flattened)[:800])
        else:
            parts.append(str(txt)[:800])
    # Belt-and-suspenders: every part must be a str before join.
    return "\n".join(str(p) for p in parts).strip()


def _format_transcript_for_call(
    transcript_text: str,
    triage: Dict[str, Any],
    *,
    max_chars: int,
) -> str:
    summary = triage.get("quick_summary") or ""
    topics = triage.get("topics") or []
    return (
        f"# Triage summary\n{summary}\n"
        f"# Topics\n{', '.join(str(t) for t in topics)}\n\n"
        f"# Transcript\n{transcript_text[:max_chars]}\n"
        "\n---\n"
        "Identify the candidate actions now. Return ONLY the JSON object "
        "with the `candidates` key per the schema in the system prompt."
    )


def _normalize_input_slots(slots: Any) -> List[Dict[str, Any]]:
    if not isinstance(slots, list):
        return []
    out = []
    for s in slots:
        if not isinstance(s, dict):
            continue
        out.append(s)
    return out


def _snapshot_external_context(ext: ExternalContextResult) -> Dict[str, Any]:
    return {
        "connected_providers": ext.connected_providers,
        "snapshots": [
            {
                "provider": snap.provider,
                "deals": snap.deals,
                "last_synced_at": (
                    snap.last_synced_at.isoformat()
                    if snap.last_synced_at
                    else None
                ),
                "is_stale": snap.is_stale,
                "error_reason": snap.error_reason,
            }
            for snap in ext.snapshots
        ],
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _summary_block_for_artifact(composition: Dict[str, Any]) -> str:
    goal = composition.get("goal") or ""
    return f"Plan goal: {goal}"


def _format_participants(participants: Any) -> str:
    if not isinstance(participants, list) or not participants:
        return "(none specified)"
    parts = []
    for p in participants[:8]:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or "(unnamed)"
        role = p.get("role") or ""
        side = p.get("side") or ""
        parts.append(f"{name} ({role}, {side})")
    return "; ".join(parts) or "(none specified)"


def _format_filled_slots(slots: Any) -> str:
    if not isinstance(slots, list) or not slots:
        return "(no input slots declared)"
    lines = []
    for s in slots:
        if not isinstance(s, dict):
            continue
        key = s.get("slot_key", "")
        val = s.get("filled_value")
        if val is None:
            lines.append(f"- {key}: <unfilled>")
        else:
            display = json.dumps(val) if not isinstance(val, str) else val
            lines.append(f"- {key}: {display}")
    return "\n".join(lines)


def _format_output_schema(schema: Any) -> str:
    if not isinstance(schema, list) or not schema:
        return "(this step does not produce reusable output)"
    lines = []
    for s in schema:
        if not isinstance(s, dict):
            continue
        lines.append(
            f"- {s.get('slot_key')} ({s.get('type', 'string')}): "
            f"{s.get('description', '')}"
        )
    return "\n".join(lines)


# ── Slot seeding from interaction ─────────────────────────────────────
#
# The synthesizer's ``input_slots`` system was always designed to fill
# from two sources: (a) upstream steps in the same plan, and (b) the
# source call's analysis insights. Path (a) is wired (see
# ``filled_by_step_index`` -> ``filled_by_step_id``). Path (b) was
# unwired until this commit — every slot the LLM declared came back
# with ``filled_value=None``, so artifact bodies surfaced literal
# placeholders like ``{contact_name}`` and ``{call_date}`` to the rep.
#
# The seed pass below maps common slot keys to interaction-derived
# values. The mapping is intentionally generous (matches multiple
# slot-key variants per concept) because the LLM emits creative
# slot names: ``contact_name``, ``prospect_name``, ``customer_contact``,
# ``decision_maker``, etc. all converge on the customer's contact name.


def _slot_key_matches(key: str, *needles: str) -> bool:
    """Lowercase substring match on slot keys for fuzzy seeding."""
    k = (key or "").lower()
    return any(n in k for n in needles)


async def _seed_slots_from_interaction(
    db: AsyncSession,
    *,
    steps: List[ActionStep],
    interaction: Interaction,
) -> int:
    """Walk every step's input_slots and fill obvious ones from the
    source interaction's data. Returns the count of slots seeded so
    callers can log it.

    Heuristic — match slot_key by substring against canonical concepts.
    Anything we can't recognize stays ``filled_value=None`` and the
    Call C placeholder system runs as today.
    """
    insights = interaction.insights or {}
    if not isinstance(insights, dict):
        insights = {}

    contact_name: Optional[str] = None
    if interaction.contact_id is not None:
        from backend.app.models import Contact as _Contact
        contact = await db.get(_Contact, interaction.contact_id)
        if contact and contact.name:
            contact_name = contact.name

    call_date: Optional[str] = None
    if interaction.created_at is not None:
        call_date = interaction.created_at.date().isoformat()

    summary: Optional[str] = (
        insights.get("summary") if isinstance(insights.get("summary"), str) else None
    )

    topics_raw = insights.get("topics") if isinstance(insights.get("topics"), list) else []
    top_topic_names: List[str] = []
    for t in topics_raw[:5]:
        if isinstance(t, dict) and t.get("name"):
            top_topic_names.append(str(t["name"]))
    topics_joined = ", ".join(top_topic_names) if top_topic_names else None

    action_items_raw = (
        insights.get("action_items") if isinstance(insights.get("action_items"), list) else []
    )
    rep_actions: List[str] = []
    next_due_date: Optional[str] = None
    for ai in action_items_raw:
        if not isinstance(ai, dict):
            continue
        title = ai.get("title")
        if isinstance(title, str) and title.strip():
            rep_actions.append(title.strip())
        if next_due_date is None:
            due = ai.get("due_date")
            if isinstance(due, str) and due.strip():
                next_due_date = due.strip()
    rep_action_items_joined: Optional[str] = None
    if rep_actions:
        rep_action_items_joined = "; ".join(rep_actions[:6])

    customer_signals = insights.get("customer_signals") if isinstance(insights.get("customer_signals"), dict) else {}
    commitment_quotes: List[str] = []
    if isinstance(customer_signals, dict):
        raw_commitments = customer_signals.get("commitment_language") or []
        if isinstance(raw_commitments, list):
            for q in raw_commitments[:4]:
                if isinstance(q, str) and q.strip():
                    commitment_quotes.append(q.strip())
    customer_commitments_joined: Optional[str] = None
    if commitment_quotes:
        customer_commitments_joined = "; ".join(commitment_quotes)

    key_moments_raw = (
        insights.get("key_moments") if isinstance(insights.get("key_moments"), list) else []
    )
    key_moments_summary: Optional[str] = None
    if key_moments_raw:
        descs: List[str] = []
        for km in key_moments_raw[:4]:
            if isinstance(km, dict):
                d = km.get("description")
                if isinstance(d, str) and d.strip():
                    descs.append(d.strip())
        if descs:
            key_moments_summary = "; ".join(descs)

    seeded = 0
    for step in steps:
        slots = step.input_slots
        if not isinstance(slots, list):
            continue
        changed = False
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            if slot.get("filled_value") is not None:
                continue  # already filled by upstream step wiring
            key = str(slot.get("slot_key") or "")
            value: Optional[str] = None

            if contact_name and _slot_key_matches(
                key, "contact_name", "prospect_name", "customer_contact",
                "customer_name", "decision_maker", "primary_contact",
                "buyer_name", "lead_name",
            ):
                value = contact_name
            elif call_date and _slot_key_matches(
                key, "call_date", "interaction_date", "conversation_date",
                "meeting_date_prior", "discovery_date",
            ):
                value = call_date
            elif next_due_date and _slot_key_matches(
                key, "due_date", "target_date", "deadline", "follow_up_date",
                "delivery_date", "by_date",
            ):
                value = next_due_date
            elif summary and _slot_key_matches(
                key, "customer_stated_need", "customer_need", "stated_need",
                "pain_point", "summary", "call_summary", "context",
                "background",
            ):
                value = summary
            elif topics_joined and _slot_key_matches(
                key, "key_topics", "topics", "topics_discussed",
                "discussion_areas", "themes", "focus_areas",
            ):
                value = topics_joined
            elif rep_action_items_joined and _slot_key_matches(
                key, "rep_action_items", "rep_actions", "next_steps",
                "internal_actions", "vendor_actions",
            ):
                value = rep_action_items_joined
            elif customer_commitments_joined and _slot_key_matches(
                key, "customer_action_items", "customer_commitments",
                "customer_actions", "customer_promises", "agreed_actions",
            ):
                value = customer_commitments_joined
            elif key_moments_summary and _slot_key_matches(
                key, "meeting_context", "key_moments", "discussion_highlights",
                "conversation_highlights",
            ):
                value = key_moments_summary

            if value is not None:
                slot["filled_value"] = value
                slot["filled_at"] = datetime.utcnow().isoformat()
                slot["filled_by_source"] = "interaction_seed"
                seeded += 1
                changed = True
        if changed:
            # SQLAlchemy needs the JSONB column reassigned to detect a
            # nested mutation; reassign the same list reference to a
            # fresh shallow copy.
            step.input_slots = list(slots)
    return seeded


def _artifact_kind_for_channel(channel: str) -> str:
    return {
        "email": "email",
        "phone_call": "script",
        "meeting": "meeting",
        "document_send": "email",
        "research": "research",
        "system_write": "system_write_payload",
        "note": "note",
    }.get(channel, "note")


__all__ = [
    "ActionPlanSynthesizer",
    "SynthesisInputs",
    "SynthesisResult",
    "SynthesisFailedError",
    "resolve_domain",
]
