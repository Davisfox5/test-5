"""Customer service domain template — post-sale support and retention."""
from __future__ import annotations

from .base import DomainTemplate, OutputSlotExample


TEMPLATE = DomainTemplate(
    name="customer_service",
    role="customer service rep",
    tone="empathetic-accountable",
    tone_description=(
        "Empathetic and accountable. Acknowledge the customer's "
        "experience without over-apologizing. Be specific about what "
        "will happen, when, and what they can expect. No corporate "
        "boilerplate; speak plainly."
    ),
    customer_endpoint_archetype="Resolution confirmation to customer",
    customer_endpoint_description=(
        "A clear close-out message to the customer that names the "
        "resolution (refund posted, ticket escalated, replacement "
        "shipped), confirms any follow-up they should expect, and "
        "leaves a clean path to re-open if anything is still off. "
        "Tone matches the call: warmer if rapport was strong, more "
        "businesslike if the call was tense."
    ),
    loop_in_role_examples=(
        "Tier 2 Support",
        "Billing",
        "Retention specialist",
        "Account Manager",
        "Trust & Safety",
        "Engineering (for confirmed bugs)",
        "Manager (for goodwill credits over policy)",
    ),
    output_slot_examples=(
        OutputSlotExample(
            "refund_amount_and_status",
            "The refund or credit amount and whether it has been "
            "issued, queued, or denied; with reference id when issued.",
        ),
        OutputSlotExample(
            "ticket_id",
            "The id of the support ticket / case opened in the "
            "ticketing system (Zendesk, HubSpot Service, etc.).",
        ),
        OutputSlotExample(
            "escalation_owner",
            "Who in T2/Engineering owns the escalation and their ETA.",
        ),
        OutputSlotExample(
            "root_cause_summary",
            "Brief root-cause explanation from the team that diagnosed "
            "the issue, written in language the customer can read.",
        ),
        OutputSlotExample(
            "retention_offer",
            "If the call was a churn risk: the offer extended (discount, "
            "downgrade, free month) and whether the customer accepted.",
        ),
        OutputSlotExample(
            "policy_exception_approval",
            "When policy was bent — manager approval and the reason "
            "logged for audit.",
        ),
    ),
    goal_examples=(
        "Resolve refund + confirm retention",
        "Close billing dispute with credit issued",
        "Escalate confirmed bug + commit on customer comms",
        "De-escalate complaint and rebuild trust",
    ),
)
