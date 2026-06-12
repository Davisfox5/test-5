"""``DomainTemplate`` — the per-domain shell consumed by the prompts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class OutputSlotExample:
    """A canonical-looking output slot from past work in this domain.

    Examples are *suggestions* to the synthesizer, never a closed set.
    Call A is told it may emit any slot key that makes sense for the
    call; these examples just anchor the vocabulary so that, across
    plans, similar concepts converge on similar slot keys (which makes
    cross-plan analytics meaningful).
    """

    slot_key: str
    description: str


@dataclass(frozen=True)
class DomainTemplate:
    """Per-domain framing for Calls A / B / C.

    Fields intentionally narrow: the load-bearing direction comes from
    the tenant's KB. The template only injects voice and provides
    example vocabulary the synthesizer can lean on when no KB
    procedure speaks to the situation.
    """

    # Canonical short name — matches ``tenants.default_domain`` /
    # ``users.default_domain`` / ``action_plans.domain``.
    name: str

    # First-person framing for the LLM — "You are a {role} reviewing a
    # call …". Direct, evocative, short.
    role: str

    # Tone tag + a short description that's interpolated into Call C's
    # artifact-rendering prompt.
    tone: str
    tone_description: str

    # Short label for the customer-facing endpoint archetype shown in
    # the UI ("Close-out email to customer", "Resolution confirmation",
    # "Fix communication"). Plus a one-sentence description fed to
    # Call B so it knows what the endpoint typically *looks like* in
    # this domain.
    customer_endpoint_archetype: str
    customer_endpoint_description: str

    # Roles the rep commonly loops in. Surfaced to Call A as examples
    # — Call A is free to emit other roles when the call requires it.
    loop_in_role_examples: Tuple[str, ...] = ()

    # Output-slot vocabulary anchors (see ``OutputSlotExample``).
    output_slot_examples: Tuple[OutputSlotExample, ...] = ()

    # Example goal strings for Call B. Lets the model converge on a
    # consistent short-form goal even across very different calls.
    goal_examples: Tuple[str, ...] = ()
