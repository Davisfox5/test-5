"""IT / technical support domain template."""
from __future__ import annotations

from .base import DomainTemplate, OutputSlotExample


TEMPLATE = DomainTemplate(
    name="it_support",
    role="technical support engineer",
    tone="precise-technical",
    tone_description=(
        "Precise and evidence-cited. State facts before opinions. "
        "Quote log lines and version strings verbatim when relevant. "
        "Be direct about what is known versus what is hypothesized. "
        "Avoid hedging; if uncertain, say so once and move on."
    ),
    customer_endpoint_archetype="Fix or workaround communication",
    customer_endpoint_description=(
        "A technical close-out to the customer: the fix or workaround, "
        "the root cause if known, the ticket reference for follow-up, "
        "and (when relevant) what to monitor going forward. Includes "
        "any required customer-side action (restart, version upgrade, "
        "configuration change). Plain language but no oversimplification."
    ),
    loop_in_role_examples=(
        "On-call engineer",
        "DevOps / SRE",
        "Platform team",
        "Security",
        "Database admin",
        "QA",
        "Engineering manager (for SLA breaches)",
    ),
    output_slot_examples=(
        OutputSlotExample(
            "ticket_id",
            "The id of the incident or bug ticket in Jira / Linear / "
            "the internal tracker.",
        ),
        OutputSlotExample(
            "repro_steps",
            "Minimal step-by-step reproduction confirmed by the team.",
        ),
        OutputSlotExample(
            "root_cause",
            "Confirmed root cause (not hypothesis) from the eng team.",
        ),
        OutputSlotExample(
            "affected_versions",
            "Which versions / commits / environments are affected.",
        ),
        OutputSlotExample(
            "fix_eta",
            "When the fix is expected to ship, with the channel "
            "(hotfix, next release, manual config change).",
        ),
        OutputSlotExample(
            "workaround",
            "Customer-actionable workaround until the fix lands.",
        ),
        OutputSlotExample(
            "log_evidence",
            "Verbatim log snippets or trace ids supporting the diagnosis.",
        ),
        OutputSlotExample(
            "sla_impact_assessment",
            "Whether the issue breached SLA and the resulting credit / "
            "service obligation, if any.",
        ),
    ),
    goal_examples=(
        "Confirm root cause and ship workaround",
        "Restore service for affected customer and schedule hotfix",
        "Escalate suspected security issue and brief customer",
        "Close ticket with runbook update for recurrence prevention",
    ),
)
