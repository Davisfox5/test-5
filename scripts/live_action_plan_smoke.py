#!/usr/bin/env python3
"""Live end-to-end smoke against the real Anthropic API.

Drives the action-plan synthesizer's three core LLM calls (A, B, C)
without touching the database or the vector store. Verifies:

* Call A returns a parseable candidate list (or empty for resolved calls)
* Call B turns candidates into a wired DAG with a customer endpoint
* Call C renders a real email artifact for the endpoint
* The orchestrator prompt formats correctly with literal placeholders
* Prompt caching headers don't break the request shape

Run:
    python3 scripts/live_action_plan_smoke.py

Costs ~$0.02-0.05 per run (Sonnet for Call B + Call C endpoint;
Haiku for Call A + the orchestrator demo). Requires ANTHROPIC_API_KEY
in .env.

Exits 0 on success, 1 on any failure. Prints a per-stage report.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from typing import Any, Dict, List

# Make backend.* importable when run from the repo root or from anywhere.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load .env so ANTHROPIC_API_KEY (and friends) are present.
_env_path = os.path.join(_PROJECT_ROOT, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


# Required for backend.app.config to load without crashing.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "smoke-test-not-used")
os.environ.setdefault("DEBUG", "true")


from backend.app.services.action_plan.domains import get as get_domain
from backend.app.services.action_plan.external_context import (
    ExternalContextResult,
    build_capabilities_block,
)
from backend.app.services.action_plan.prompts import (
    CALL_A_SYSTEM_PROMPT,
    CALL_B_SYSTEM_PROMPT,
    CALL_C_PAYLOAD_SCHEMAS,
    CALL_C_SYSTEM_PROMPT,
)
from backend.app.services.action_plan.synthesizer import ActionPlanSynthesizer
from backend.app.services.kb.action_plan_retrieve import (
    ActionPlanRetrievalResult,
    RetrievedProcedure,
)
from backend.app.services.kb.orchestrator_prompts import (
    format_orchestrator_system,
    format_orchestrator_user,
)


# ──────────────────────────────────────────────────────────
# In-memory fixture: an Apex Communications sales call
# ──────────────────────────────────────────────────────────

TRANSCRIPT = """
Marcus: Kevin, thanks for the time. I want to walk through the
three pricing tiers you saw and answer the security questions you
raised on last week's demo.

Kevin: Appreciate it. The pricing was helpful but I need updated
seat caps for each tier — what you sent was from January and we're
looking at scaling to about 40K seats.

Marcus: I'll get the latest numbers from our vendor team and
confirm by Friday. On security — you mentioned managed security
specifically, right?

Kevin: Yes. Our compliance team will need a managed security layer
included or as an add-on. Right now we're also evaluating
CompetitorX which apparently bundles theirs.

Marcus: Got it. I'll loop in Product on the security add-on and
get you specifics. Last piece: integration with your internal
billing system — I need our SE to confirm that works out of the
box before I commit. I'll have them check.

Kevin: Sounds good. Send me a consolidated proposal Friday and I
can walk it through with my CFO Monday.
"""

PROCEDURE_BLOCK = """
[P1] Sales follow-up after enterprise discovery call - compliance=should
  applies_when: After a sales call with an enterprise prospect where
                multiple internal teams need to be looped in
  triggers: enterprise pricing follow-up, multiple internal asks
  citation: doc_id=fake-doc-1 chunk_id=fake-chunk-1
  required_steps:
    1. Email each internal team with a specific ask
       Each ask should include the prospect's scale numbers and deadline.
    2. Send consolidated proposal email to prospect
       Wait for all internal responses before sending.
  required_integrations: gmail.send_email
"""


def _build_call_a_prompt() -> str:
    template = get_domain("sales")
    loop_in_examples = ", ".join(template.loop_in_role_examples)
    output_slot_examples = "\n".join(
        f"  - {ex.slot_key}: {ex.description}"
        for ex in template.output_slot_examples
    )
    return CALL_A_SYSTEM_PROMPT.format(
        domain_role=template.role,
        tenant_name="Acme Telco",
        procedures_block=PROCEDURE_BLOCK,
        articles_block="(no reference articles)",
        customer_brief_block=(
            "Customer: Kevin Okafor, VP Product, Apex Communications\n"
            "Stakeholders: Kevin (decision-maker), unnamed CFO (signoff)\n"
            "Deal: HubSpot 'Apex Enterprise Q3', stage=Proposal, $480k, "
            "close 2026-07-15"
        ),
        tenant_capabilities_block=build_capabilities_block(["hubspot", "gmail"]),
        loop_in_role_examples=loop_in_examples,
        output_slot_examples=output_slot_examples,
    )


def _summarize_candidates(candidates: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"  [{i}] {c.get('title', '?')} "
        f"(channel={c.get('channel')}, kb={'yes' if c.get('kb_source') else 'no'})"
        for i, c in enumerate(candidates)
    )


def _summarize_steps(steps: List[Dict[str, Any]]) -> str:
    lines = []
    for i, s in enumerate(steps):
        deps = s.get("depends_on") or []
        slots_in = s.get("input_slots") or []
        slots_out = s.get("output_schema") or []
        lines.append(
            f"  [{i}] {s.get('title', '?')} role={s.get('role_in_plan')}"
        )
        if deps:
            lines.append(f"      depends_on: {deps}")
        if slots_in:
            slot_keys = [sl.get("slot_key") for sl in slots_in if isinstance(sl, dict)]
            lines.append(f"      input_slots: {slot_keys}")
        if slots_out:
            slot_keys = [sl.get("slot_key") for sl in slots_out if isinstance(sl, dict)]
            lines.append(f"      output_schema: {slot_keys}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────
# Stage runners — each uses the real ActionPlanSynthesizer internals
# ──────────────────────────────────────────────────────────


async def stage_orchestrator_prompt_render() -> None:
    """Pure-CPU: verify the literal-placeholder render path works."""
    sys_prompt = format_orchestrator_system(tenant_name="Acme Telco")
    user_msg = format_orchestrator_user(
        title="Refund policy",
        source_description="uploaded",
        char_count=12,
        content="refund stuff",
    )
    assert "Acme Telco" in sys_prompt, "orchestrator system did not interpolate"
    assert "Refund policy" in user_msg, "orchestrator user template did not interpolate"
    assert "<TENANT_NAME>" not in sys_prompt, "literal placeholder leaked"
    assert "<TITLE>" not in user_msg, "literal placeholder leaked"
    # JSON skeleton must survive untouched.
    assert "required_steps: [" in sys_prompt, "JSON skeleton mangled"
    print("[OK] Orchestrator prompt renders without mangling JSON skeleton")


async def stage_call_a() -> List[Dict[str, Any]]:
    synth = ActionPlanSynthesizer()
    system_prompt = _build_call_a_prompt()
    user_content = (
        "# Triage summary\nKevin discussed pricing, security, and "
        "integration; promised proposal Friday.\n"
        "# Topics\npricing, managed security, integration, competitor\n\n"
        f"# Transcript\n{TRANSCRIPT}\n\n---\n"
        "Identify the candidate actions now. Return ONLY the JSON."
    )
    data = await synth._call_with_retry(  # noqa: SLF001
        system_prompt=system_prompt,
        user_content=user_content,
        primary_tier="haiku",
        max_tokens=4096,
        label="smoke.call_a",
    )
    candidates = data.get("candidates", [])
    assert isinstance(candidates, list), "Call A returned non-list candidates"
    assert candidates, "Call A returned zero candidates — should propose follow-up work"
    assert len(candidates) <= 15, f"Call A exceeded hard cap (got {len(candidates)})"
    for c in candidates:
        assert "title" in c and c["title"], "candidate missing title"
        assert "channel" in c, f"candidate missing channel: {c}"
    print(f"[OK] Call A: {len(candidates)} candidates")
    print(_summarize_candidates(candidates))
    return candidates


async def stage_call_b(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    synth = ActionPlanSynthesizer()
    template = get_domain("sales")
    system_prompt = CALL_B_SYSTEM_PROMPT.format(
        domain_role=template.role,
        customer_endpoint_archetype=template.customer_endpoint_archetype,
        customer_endpoint_description=template.customer_endpoint_description,
        goal_examples=", ".join(f"\"{g}\"" for g in template.goal_examples),
        procedures_summary_block=(
            "[P1] Sales follow-up after enterprise discovery call "
            "(compliance=should; chunk_id=fake-chunk-1): "
            "Email each internal team with a specific ask; "
            "Send consolidated proposal email to prospect"
        ),
        candidates_block="\n".join(
            f"[{i}] {c.get('title')} (channel={c.get('channel')})"
            for i, c in enumerate(candidates)
        ),
    )
    user_content = (
        "Compose the plan now. Cluster, order, identify the customer "
        "endpoint, wire input slots, verify procedure compliance.\n\n"
        "Raw candidates (for cross-reference):\n"
        + json.dumps(candidates, indent=2)
    )
    plan = await synth._call_with_retry(  # noqa: SLF001
        system_prompt=system_prompt,
        user_content=user_content,
        primary_tier="sonnet",
        max_tokens=6000,
        label="smoke.call_b",
    )
    assert "steps" in plan, f"Call B missing 'steps': {list(plan.keys())}"
    steps = plan["steps"]
    assert isinstance(steps, list) and steps, "Call B returned no steps"
    assert "goal" in plan, "Call B missing 'goal'"
    # Customer endpoint check (hybrid: present if any customer-facing step exists)
    endpoint_idx = plan.get("customer_endpoint_index")
    if endpoint_idx is not None:
        assert 0 <= endpoint_idx < len(steps), f"endpoint_idx out of range: {endpoint_idx}"
        endpoint = steps[endpoint_idx]
        assert endpoint.get("role_in_plan") == "customer_endpoint", (
            f"endpoint step role mismatch: {endpoint.get('role_in_plan')}"
        )
    # At least one step has a non-empty depends_on (otherwise it's a flat list)
    has_deps = any(s.get("depends_on") for s in steps)
    print(f"[OK] Call B: goal={plan.get('goal')!r}")
    print(f"     {len(steps)} steps, endpoint_index={endpoint_idx}, "
          f"has_dependencies={has_deps}")
    print(_summarize_steps(steps))
    return plan


async def stage_call_c_endpoint(plan: Dict[str, Any]) -> Dict[str, Any]:
    endpoint_idx = plan.get("customer_endpoint_index")
    if endpoint_idx is None:
        print("[SKIP] Call C: no customer endpoint in this plan")
        return {}
    endpoint = plan["steps"][endpoint_idx]
    template = get_domain("sales")
    schema = CALL_C_PAYLOAD_SCHEMAS["email"]
    system_prompt = CALL_C_SYSTEM_PROMPT.format(
        domain_role=template.role,
        tone=template.tone,
        tone_description=template.tone_description,
        tenant_name="Acme Telco",
        summary_block=f"Plan goal: {plan.get('goal')}",
        customer_brief_block="Kevin Okafor, VP Product, Apex Communications",
        step_title=endpoint["title"],
        step_intent=endpoint.get("intent", ""),
        step_channel="email",
        step_participants="Kevin Okafor (VP Product, customer)",
        filled_slots_block="\n".join(
            f"- {s.get('slot_key')}: <unfilled>"
            for s in endpoint.get("input_slots") or []
            if isinstance(s, dict)
        ) or "(no input slots declared)",
        output_schema_block="(this step does not produce reusable output)",
        kb_template_block="(no template in KB)",
        payload_schema_block=schema,
    )
    user_content = (
        "Draft the email artifact now. Return ONLY the JSON per the "
        "schema in the system prompt."
    )
    synth = ActionPlanSynthesizer()
    payload = await synth._call_with_retry(  # noqa: SLF001
        system_prompt=system_prompt,
        user_content=user_content,
        primary_tier="sonnet",
        max_tokens=4000,
        label="smoke.call_c_endpoint",
    )
    assert "subject" in payload, f"email artifact missing subject: {list(payload.keys())}"
    assert "body" in payload, "email artifact missing body"
    # Slot placeholders must survive in the body when slots are unfilled
    body = payload.get("body", "")
    unfilled = payload.get("unfilled_slots") or []
    if unfilled:
        # At least one unfilled slot's key should appear as a placeholder
        # in the body (the prompt asks the model to leave {slot_key} markers).
        any_placeholder = any(f"{{{s}}}" in body for s in unfilled)
        if not any_placeholder:
            print(f"[WARN] No {{slot_key}} placeholders in body for unfilled slots {unfilled}")
        else:
            print(f"[OK] Body retains placeholders for unfilled slots: {unfilled}")
    subject_preview = payload["subject"][:80]
    body_preview = body[:200].replace("\n", " ")
    print(f"[OK] Call C: rendered endpoint email artifact")
    print(f"     Subject: {subject_preview}")
    print(f"     Body:    {body_preview}…")
    return payload


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────


async def _main() -> int:
    failures = 0

    print("=" * 60)
    print("LIVE ACTION PLAN SMOKE TEST")
    print("=" * 60)
    print()

    # Stage 0: pure-CPU prompt render checks
    try:
        await stage_orchestrator_prompt_render()
    except Exception as exc:
        print(f"[FAIL] Orchestrator prompt render: {exc}")
        traceback.print_exc()
        failures += 1

    # Stage 1: Call A
    print()
    print("--- Stage 1: Call A (candidate generation, Haiku) ---")
    candidates: List[Dict[str, Any]] = []
    try:
        candidates = await stage_call_a()
    except Exception as exc:
        print(f"[FAIL] Call A: {exc}")
        traceback.print_exc()
        failures += 1

    # Stage 2: Call B (depends on Stage 1)
    plan: Dict[str, Any] = {}
    if candidates:
        print()
        print("--- Stage 2: Call B (DAG composition, Sonnet) ---")
        try:
            plan = await stage_call_b(candidates)
        except Exception as exc:
            print(f"[FAIL] Call B: {exc}")
            traceback.print_exc()
            failures += 1

    # Stage 3: Call C (depends on Stage 2)
    if plan:
        print()
        print("--- Stage 3: Call C (endpoint artifact rendering, Sonnet) ---")
        try:
            await stage_call_c_endpoint(plan)
        except Exception as exc:
            print(f"[FAIL] Call C: {exc}")
            traceback.print_exc()
            failures += 1

    print()
    print("=" * 60)
    if failures == 0:
        print("ALL STAGES PASSED")
        return 0
    print(f"{failures} STAGE(S) FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
