"""Generic fallback domain template.

Used when the tenant hasn't declared a primary domain and triage
isn't confident enough to override. Stays deliberately neutral —
no domain-flavored vocabulary, no domain-flavored tone — so the
KB and the call itself do all the steering.
"""
from __future__ import annotations

from .base import DomainTemplate, OutputSlotExample


TEMPLATE = DomainTemplate(
    name="generic",
    role="rep",
    tone="neutral-professional",
    tone_description=(
        "Neutral and professional. Match the customer's register. "
        "Specific over generic; evidence-cited; no filler praise."
    ),
    customer_endpoint_archetype="Wrap-up message to customer",
    customer_endpoint_description=(
        "A close-out message to the customer that summarizes what was "
        "agreed on the call and names a single concrete next step."
    ),
    loop_in_role_examples=(
        "Account owner",
        "Manager",
        "Subject-matter expert",
    ),
    output_slot_examples=(
        OutputSlotExample(
            "follow_up_summary",
            "What was promised to the customer.",
        ),
        OutputSlotExample(
            "open_question_resolution",
            "Answer to a question the rep couldn't answer on the call.",
        ),
        OutputSlotExample(
            "internal_owner",
            "Who internally owns this thread going forward.",
        ),
    ),
    goal_examples=(
        "Close out and confirm next step",
        "Resolve open question and follow up",
    ),
)
