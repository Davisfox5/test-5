"""Prompt smoke tests — format keys, JSON-only directives, voice rules."""

from __future__ import annotations

import string

import pytest

from backend.app.services.action_plan.prompts import (
    ACTION_PLAN_PROMPT_VERSION,
    CALL_A_SYSTEM_PROMPT,
    CALL_B_SYSTEM_PROMPT,
    CALL_C_PAYLOAD_SCHEMAS,
    CALL_C_SYSTEM_PROMPT,
    CALL_D_SYSTEM_PROMPT,
)
from backend.app.services.kb.orchestrator_prompts import (
    ORCHESTRATOR_PROMPT_VERSION,
    ORCHESTRATOR_SYSTEM_PROMPT,
    format_orchestrator_system,
    format_orchestrator_user,
)


def _format_keys(template: str) -> set:
    """Return all single-brace ``{name}`` interpolation keys in a template.

    Skips ``{{`` / ``}}`` doubled braces (which Python format treats as
    literal braces — used in our JSON skeletons).
    """
    keys = set()
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name:
            keys.add(field_name)
    return keys


# ──────────────────────────────────────────────────────────
# Prompt versions
# ──────────────────────────────────────────────────────────


def test_prompt_versions_present():
    assert ACTION_PLAN_PROMPT_VERSION
    assert ORCHESTRATOR_PROMPT_VERSION


# ──────────────────────────────────────────────────────────
# Format key coverage — every {placeholder} must be supplied by callers
# ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt,expected",
    [
        (
            CALL_A_SYSTEM_PROMPT,
            {
                "domain_role",
                "tenant_name",
                "procedures_block",
                "articles_block",
                "customer_brief_block",
                "tenant_capabilities_block",
                "loop_in_role_examples",
                "output_slot_examples",
            },
        ),
        (
            CALL_B_SYSTEM_PROMPT,
            {
                "domain_role",
                "customer_endpoint_archetype",
                "customer_endpoint_description",
                "goal_examples",
                "procedures_summary_block",
                "candidates_block",
            },
        ),
        (
            CALL_C_SYSTEM_PROMPT,
            {
                "domain_role",
                "tone",
                "tone_description",
                "tenant_name",
                "summary_block",
                "customer_brief_block",
                "step_title",
                "step_intent",
                "step_channel",
                "step_participants",
                "filled_slots_block",
                "output_schema_block",
                "kb_template_block",
                "payload_schema_block",
            },
        ),
        (
            CALL_D_SYSTEM_PROMPT,
            {
                "source_label",
                "step_title",
                "step_intent",
                "output_schema_block",
                "source_content",
            },
        ),
    ],
)
def test_format_keys_match_expected(prompt, expected):
    actual = _format_keys(prompt)
    assert actual == expected, (
        f"Format keys drifted from caller expectations.\n"
        f"Missing in template: {expected - actual}\n"
        f"Unexpected in template: {actual - expected}"
    )


# ──────────────────────────────────────────────────────────
# Voice rules — em-dash ban is load-bearing for the project; the
# action-plan prompts must keep enforcing it.
# ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [CALL_A_SYSTEM_PROMPT, CALL_B_SYSTEM_PROMPT, CALL_C_SYSTEM_PROMPT],
)
def test_no_em_dashes_in_prompt_body(prompt):
    """The shared _VOICE_RULES block bans em-dashes; the prompts that
    interpolate it must not themselves drift and emit em-dashes (the
    earliest prompts in the project did, and this regressed quality).
    """
    assert "—" not in prompt, "em-dash (—) found in prompt"
    assert "–" not in prompt, "en-dash (–) found in prompt"


@pytest.mark.parametrize(
    "prompt",
    [CALL_A_SYSTEM_PROMPT, CALL_B_SYSTEM_PROMPT, CALL_C_SYSTEM_PROMPT, CALL_D_SYSTEM_PROMPT],
)
def test_prompt_demands_only_json_output(prompt):
    """Every call must instruct the model to return ONLY JSON, no fences.
    Without this the json_repair fallback gets hit on most responses.
    """
    lowered = prompt.lower()
    assert "return only" in lowered or "only json" in lowered or "only valid json" in lowered


# ──────────────────────────────────────────────────────────
# Call A specifics — load-bearing rules from the design
# ──────────────────────────────────────────────────────────


def test_call_a_allows_empty_candidates():
    """Without this instruction the model fabricates filler steps to
    satisfy the schema's `candidates: [...]` field."""
    assert "return {{ \"candidates\": [] }}" in CALL_A_SYSTEM_PROMPT or \
        "candidates\": []" in CALL_A_SYSTEM_PROMPT


def test_call_a_cap_is_explicit():
    assert "HARD CAP" in CALL_A_SYSTEM_PROMPT or "Hard cap" in CALL_A_SYSTEM_PROMPT
    assert "15 candidates" in CALL_A_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────
# Call B specifics — KB compliance check rule
# ──────────────────────────────────────────────────────────


def test_call_b_forbids_dropping_required_steps():
    """Procedure-required steps cannot be dropped — locked decision."""
    assert "may NOT silently drop" in CALL_B_SYSTEM_PROMPT or \
        "MUST appear" in CALL_B_SYSTEM_PROMPT or \
        "must restore" in CALL_B_SYSTEM_PROMPT.lower()


def test_call_b_emits_compliance_audit():
    """The compliance_audit field is how we verify Call B's adherence."""
    assert "compliance_audit" in CALL_B_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────
# Call C payload schemas — one per channel; each must declare unfilled_slots
# ──────────────────────────────────────────────────────────


def test_payload_schemas_cover_all_channels():
    assert set(CALL_C_PAYLOAD_SCHEMAS.keys()) == {
        "email",
        "phone_call",
        "meeting",
        "document_send",
        "research",
        "system_write",
        "note",
    }


@pytest.mark.parametrize("channel,schema", list(CALL_C_PAYLOAD_SCHEMAS.items()))
def test_every_payload_schema_includes_unfilled_slots(channel, schema):
    """The synthesizer + UI both rely on unfilled_slots to render
    placeholders in the terminal artifact."""
    assert "unfilled_slots" in schema, channel


# ──────────────────────────────────────────────────────────
# Call D specifics — source_quote per slot is the trust signal
# ──────────────────────────────────────────────────────────


def test_call_d_demands_source_quotes_per_filled_slot():
    assert "source_quote" in CALL_D_SYSTEM_PROMPT


def test_call_d_warns_about_quoted_history():
    """Inbound replies often carry the entire prior thread; without this
    instruction the model extracts from the outbound text we just sent."""
    lowered = CALL_D_SYSTEM_PROMPT.lower()
    assert "quoted history" in lowered or "quoted" in lowered


# ──────────────────────────────────────────────────────────
# Orchestrator — kind precedence + integration filtering
# ──────────────────────────────────────────────────────────


def test_orchestrator_lists_all_block_kinds():
    for kind in (
        "procedure",
        "policy",
        "escalation_path",
        "template",
        "context",
        "faq",
        "glossary",
        "contact_directory",
    ):
        assert kind in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_declares_precedence_when_ambiguous():
    """Without this, ambiguous prose tends to land as 'context' and
    procedures get under-extracted."""
    assert "precedence" in ORCHESTRATOR_SYSTEM_PROMPT.lower() or \
        "prefer the more specific" in ORCHESTRATOR_SYSTEM_PROMPT.lower()


def test_orchestrator_renders_with_a_real_doc():
    """Smoke: the orchestrator user template must render cleanly with
    the values the service passes. The helper uses literal placeholder
    substitution rather than ``.format()`` because the system prompt
    embeds JSON skeletons with single braces."""
    formatted = format_orchestrator_user(
        title="Refund policy",
        source_description="uploaded",
        char_count=42,
        content="When a customer requests a refund...",
    )
    assert "Refund policy" in formatted
    assert "uploaded" in formatted
    assert "42 characters" in formatted
    # No leftover placeholders
    assert "<TITLE>" not in formatted
    assert "<CONTENT>" not in formatted


def test_orchestrator_system_renders_without_clobbering_json():
    """The system prompt must keep its JSON skeleton intact after
    tenant-name substitution. Regression guard: an earlier version used
    str.format and crashed because the JSON braces look like format
    keys."""
    rendered = format_orchestrator_system(tenant_name="Acme Corp")
    assert "Acme Corp" in rendered
    assert "<TENANT_NAME>" not in rendered
    # JSON skeletons survive (un-mangled braces).
    assert "required_steps: [" in rendered
    assert "triggers: [str]" in rendered


def test_orchestrator_prompt_has_no_em_dashes():
    """Voice rule applies to every prompt the project ships."""
    assert "—" not in ORCHESTRATOR_SYSTEM_PROMPT
    assert "–" not in ORCHESTRATOR_SYSTEM_PROMPT
