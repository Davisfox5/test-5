"""Sales domain template — prospecting through close."""
from __future__ import annotations

from .base import DomainTemplate, OutputSlotExample


TEMPLATE = DomainTemplate(
    name="sales",
    role="sales rep",
    tone="consultative",
    tone_description=(
        "Consultative, persuasive, low-pressure. Lead with the "
        "customer's stated need, not the product. Specific over "
        "generic. No filler praise, no hedging."
    ),
    customer_endpoint_archetype="Advance email to prospect",
    customer_endpoint_description=(
        "A wrap-up email back to the prospect that recaps the call's "
        "decisions, attaches whatever was promised, and lands one "
        "concrete next step (a calendar invite, a counter-proposal, "
        "a request for a stakeholder intro). Tone matches the call "
        "and the customer's communication style."
    ),
    loop_in_role_examples=(
        "Sales Engineer",
        "Legal",
        "Solutions Architect",
        "Product Marketing",
        "Customer Success (for expansion handoff)",
        "Deal Desk",
        "VP Sales (for approvals)",
    ),
    output_slot_examples=(
        OutputSlotExample(
            "pricing_tier_quotes",
            "Numeric pricing for each tier the prospect is evaluating.",
        ),
        OutputSlotExample(
            "security_compliance_answers",
            "Specific answers to the prospect's security questionnaire "
            "(SOC 2, HIPAA, FedRAMP, etc.) — typically from Legal or Security.",
        ),
        OutputSlotExample(
            "integration_feasibility",
            "Whether the integration the prospect asked about is "
            "supported, with effort estimate from the SE.",
        ),
        OutputSlotExample(
            "legal_approval",
            "Legal's verdict on a contract clause the prospect raised.",
        ),
        OutputSlotExample(
            "competitor_differentiation",
            "Talking points distinguishing us from a competitor the "
            "prospect named.",
        ),
        OutputSlotExample(
            "stakeholder_intro",
            "Who else at the prospect's org should be on the next call "
            "(name, role, why they matter).",
        ),
    ),
    goal_examples=(
        "Advance Apex deal to procurement",
        "Get exec sponsor confirmed and a security review scheduled",
        "Close starter-tier upgrade by month-end",
        "Recover stalled deal with new pricing structure",
    ),
)
