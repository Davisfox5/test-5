#!/usr/bin/env python3
"""Seed a single demo Action Plan against the main app schema.

Use this AFTER a full reseed of the app to populate one example plan
so the frontend canvas has something to render for visual review.

The seeded plan models the scenario discussed during design: a sales
call with a prospect (Kevin Okafor at Apex Communications) where the
rep needs to loop in IT, loop in Product for managed security,
research a competitor, then close out the customer with consolidated
pricing + the answers from those upstream asks.

Run:
    python3 -m backend.scripts.seed_action_plan_demo --tenant-slug demo

Idempotent on (tenant_id, interaction_id): re-running upserts the same
plan rather than creating duplicates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select

from backend.app.db import async_session
from backend.app.models import (
    ActionPlan,
    ActionStep,
    Customer,
    Interaction,
    StepArtifact,
    StepResponse,
    Tenant,
)


DEMO_GOAL = "Advance Apex deal: close-out email with pricing + security + IT confirmation"


async def _resolve_tenant(db, tenant_slug: str) -> Tenant:
    rows = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = rows.scalar_one_or_none()
    if tenant is None:
        raise SystemExit(
            f"Tenant slug {tenant_slug!r} not found. Reseed the app first."
        )
    return tenant


async def _upsert_demo_customer(db, tenant: Tenant) -> Customer:
    rows = await db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant.id,
            Customer.name == "Apex Communications (demo)",
        )
    )
    customer = rows.scalar_one_or_none()
    if customer is not None:
        return customer
    customer = Customer(
        tenant_id=tenant.id,
        name="Apex Communications (demo)",
        industry="Telecom",
        domain="apex.example",
    )
    db.add(customer)
    await db.flush()
    return customer


async def _upsert_demo_interaction(
    db, tenant: Tenant, customer: Customer,
) -> Interaction:
    rows = await db.execute(
        select(Interaction).where(
            Interaction.tenant_id == tenant.id,
            Interaction.title == "Demo: Apex pricing + security discovery",
        )
    )
    interaction = rows.scalar_one_or_none()
    if interaction is not None:
        return interaction
    interaction = Interaction(
        tenant_id=tenant.id,
        channel="voice",
        title="Demo: Apex pricing + security discovery",
        customer_id=customer.id,
        raw_text=(
            "Rep: Walked Kevin through the three pricing tiers. He pressed on "
            "managed security and asked whether the integration with their "
            "internal billing system would work out of the box. Mentioned "
            "they're also evaluating CompetitorX. Promised a full proposal "
            "by end of week."
        ),
        insights={
            "summary": (
                "Sales-cycle call with Kevin Okafor (VP Product, Apex "
                "Communications). Three open threads at close: pricing "
                "structure pending updated tier limits from the vendor "
                "team, managed security layer pending Product team input, "
                "integration feasibility pending IT team confirmation."
            ),
        },
        status="processed",
    )
    db.add(interaction)
    await db.flush()
    return interaction


def _new_step_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_plan(
    db, tenant: Tenant, customer: Customer, interaction: Interaction,
) -> ActionPlan:
    # Remove any prior demo plan for this interaction (idempotency).
    existing = await db.execute(
        select(ActionPlan).where(
            ActionPlan.interaction_id == interaction.id,
        )
    )
    prior = existing.scalar_one_or_none()
    if prior is not None:
        await db.delete(prior)
        await db.flush()

    plan = ActionPlan(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        customer_id=customer.id,
        goal=DEMO_GOAL,
        domain="sales",
        status="active",
        procedures_applied=[],
        external_context_snapshot={
            "connected_providers": ["hubspot"],
            "snapshots": [
                {
                    "provider": "hubspot",
                    "deals": [
                        {
                            "external_id": "deal-apex-q3",
                            "title": "Apex Enterprise Q3",
                            "stage": "Proposal",
                            "amount": 480_000,
                            "currency": "USD",
                            "close_date": "2026-07-15",
                            "owner_name": "Marcus Tan",
                        }
                    ],
                    "last_synced_at": datetime.now(timezone.utc).isoformat(),
                    "is_stale": False,
                    "error_reason": None,
                }
            ],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(plan)
    await db.flush()

    # ── Steps ────────────────────────────────────────────────
    it_id = _new_step_id()
    product_id = _new_step_id()
    vendor_id = _new_step_id()
    research_id = _new_step_id()
    endpoint_id = _new_step_id()
    crm_log_id = _new_step_id()

    steps_to_add: List[ActionStep] = []

    # Prep step: email IT
    steps_to_add.append(
        ActionStep(
            id=it_id,
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Email IT to confirm Apex billing-system integration",
            description=(
                "Kevin asked whether the integration with their billing "
                "platform will work; needs IT to confirm before close-out."
            ),
            intent="Get a yes/no on integration feasibility from IT.",
            priority="high",
            recommended_channel="email",
            channel_reasoning=(
                "Async ask; technical answer better in writing for the file."
            ),
            participants=[
                {
                    "name": "Sarah Chen",
                    "role": "Sales Engineer",
                    "side": "vendor",
                    "source": "inferred_from_topic",
                },
            ],
            prep_artifacts=["Apex's billing platform name", "Integration spec PDF"],
            state="awaiting_response",
            depends_on=[],
            input_slots=[],
            output_schema=[
                {
                    "slot_key": "integration_feasibility",
                    "description": "Whether IT confirmed integration works out-of-the-box.",
                    "type": "string",
                }
            ],
            output_data={},
            role_in_plan="preparation",
            artifact_version=1,
            artifact_stale=False,
            started_at=datetime.now(timezone.utc),
        )
    )

    # Prep step: email Product for managed security
    steps_to_add.append(
        ActionStep(
            id=product_id,
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Email Product team about managed security layer",
            description=(
                "Kevin asked whether we can add a managed security layer to "
                "their package. Need Product to confirm SKU + monthly add-on."
            ),
            intent="Get pricing + availability for managed security.",
            priority="high",
            recommended_channel="email",
            channel_reasoning=(
                "Cross-functional ask; written response keeps everyone aligned."
            ),
            participants=[
                {
                    "name": "Product Marketing",
                    "role": "Product team",
                    "side": "vendor",
                    "source": "inferred_from_topic",
                },
            ],
            prep_artifacts=["Customer scale numbers (40K seats)", "Compliance requirements list"],
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[
                {
                    "slot_key": "managed_security_options",
                    "description": "Available managed-security tiers + monthly add-on price.",
                    "type": "string",
                }
            ],
            output_data={},
            role_in_plan="preparation",
            artifact_version=1,
            artifact_stale=False,
        )
    )

    # Prep step: email Vendor for tier limits
    steps_to_add.append(
        ActionStep(
            id=vendor_id,
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Email Vendor for updated package tier limits",
            description="Pricing-tier limits are stale in the deck Kevin saw. Need latest.",
            intent="Get the current seat/usage caps per tier.",
            priority="medium",
            recommended_channel="email",
            channel_reasoning="Routine ask; written answer is the deliverable.",
            participants=[
                {
                    "name": "Vendor Pricing",
                    "role": "Vendor team",
                    "side": "vendor",
                    "source": "inferred_from_topic",
                },
            ],
            prep_artifacts=["Latest pricing sheet"],
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[
                {
                    "slot_key": "tier_limits",
                    "description": "Seat / usage caps per tier (T1, T2, T3).",
                    "type": "string",
                }
            ],
            output_data={},
            role_in_plan="preparation",
            artifact_version=1,
            artifact_stale=False,
        )
    )

    # Prep step: research competitor
    steps_to_add.append(
        ActionStep(
            id=research_id,
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Research CompetitorX's enterprise telecom offering",
            description=(
                "Kevin mentioned evaluating CompetitorX. Need a 1-pager on how "
                "our managed-security + integration story compares."
            ),
            intent="Arm the close-out email with one differentiator.",
            priority="medium",
            recommended_channel="research",
            channel_reasoning="Pre-work; no external comms required.",
            participants=[],
            prep_artifacts=["Internal battlecards", "Public case-study list"],
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[
                {
                    "slot_key": "competitor_differentiation",
                    "description": "One clear point that beats CompetitorX in this account.",
                    "type": "string",
                }
            ],
            output_data={},
            role_in_plan="preparation",
            artifact_version=1,
            artifact_stale=False,
        )
    )

    # Customer endpoint
    steps_to_add.append(
        ActionStep(
            id=endpoint_id,
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Send close-out email to Kevin with pricing + security + IT confirmation",
            description=(
                "Consolidates pricing tiers, managed-security options, IT "
                "integration confirmation, and the competitor differentiator "
                "into one proposal email ready for Kevin to forward to his CFO."
            ),
            intent="Move the deal to procurement review.",
            priority="high",
            recommended_channel="email",
            channel_reasoning=(
                "Customer asked for a written proposal he can forward."
            ),
            participants=[
                {
                    "name": "Kevin Okafor",
                    "role": "VP Product",
                    "side": "customer",
                    "source": "named_in_call",
                },
            ],
            prep_artifacts=[],
            state="blocked",
            depends_on=[str(it_id), str(product_id), str(vendor_id), str(research_id)],
            input_slots=[
                {
                    "slot_key": "integration_feasibility",
                    "description": "Whether IT confirmed integration works.",
                    "required": True,
                    "filled_by_step_id": str(it_id),
                    "filled_value": None,
                    "filled_at": None,
                },
                {
                    "slot_key": "managed_security_options",
                    "description": "Managed security tiers + price.",
                    "required": True,
                    "filled_by_step_id": str(product_id),
                    "filled_value": None,
                    "filled_at": None,
                },
                {
                    "slot_key": "tier_limits",
                    "description": "Current pricing-tier limits.",
                    "required": True,
                    "filled_by_step_id": str(vendor_id),
                    "filled_value": None,
                    "filled_at": None,
                },
                {
                    "slot_key": "competitor_differentiation",
                    "description": "Our differentiator vs CompetitorX.",
                    "required": False,
                    "filled_by_step_id": str(research_id),
                    "filled_value": None,
                    "filled_at": None,
                },
            ],
            output_schema=[],
            output_data={},
            role_in_plan="customer_endpoint",
            artifact_version=1,
            artifact_stale=False,
        )
    )

    # Post-completion: log to CRM
    steps_to_add.append(
        ActionStep(
            id=crm_log_id,
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Log proposal sent in HubSpot (advance deal stage)",
            description="After the proposal goes out, move the HubSpot deal to 'Proposal Sent'.",
            intent="Keep the CRM source-of-truth in sync.",
            priority="low",
            recommended_channel="system_write",
            channel_reasoning="Automatable; saves manual CRM work.",
            participants=[],
            prep_artifacts=[],
            state="blocked",
            depends_on=[str(endpoint_id)],
            input_slots=[],
            output_schema=[],
            output_data={},
            role_in_plan="post_completion",
            target_integration="hubspot",
            integration_operation="update_deal_stage",
            artifact_version=1,
            artifact_stale=False,
        )
    )

    for s in steps_to_add:
        db.add(s)
    await db.flush()

    plan.customer_endpoint_step_id = endpoint_id

    # ── Artifacts (one per step at v1) ──────────────────────
    artifacts = [
        (it_id, "email", {
            "subject": "Apex - integration feasibility check (closing this week)",
            "body": (
                "Sarah,\n\nKevin Okafor at Apex (40K seats, telecom) "
                "asked whether our platform integrates with their internal "
                "billing system out-of-the-box. Could you confirm by Thursday? "
                "I'm aiming to send the close-out proposal Friday.\n\nThanks,\nMarcus"
            ),
            "cc": [],
            "bcc": [],
            "unfilled_slots": [],
        }),
        (product_id, "email", {
            "subject": "Managed security add-on for Apex Communications deal",
            "body": (
                "Hi Product team,\n\nKevin Okafor at Apex asked about a "
                "managed security layer on top of the enterprise package "
                "(40K seats, telecom). Could you share what tier options "
                "we have and pricing? Need by Thursday EOD for a Friday "
                "close-out email.\n\nThanks,\nMarcus"
            ),
            "cc": [],
            "bcc": [],
            "unfilled_slots": [],
        }),
        (vendor_id, "email", {
            "subject": "Latest tier limits for telecom enterprise pricing",
            "body": (
                "Hi vendor team,\n\nCould you send me the current seat / "
                "usage caps for tiers T1, T2, T3 of the enterprise telecom "
                "package? Pricing deck Kevin saw appears stale.\n\nThanks,\nMarcus"
            ),
            "cc": [],
            "bcc": [],
            "unfilled_slots": [],
        }),
        (research_id, "research", {
            "starting_points": [
                {
                    "url_or_source": "Internal: competitorx-battlecard.md",
                    "why": "Our maintained one-pager — start here.",
                },
                {
                    "url_or_source": "https://competitorx.example/case-studies/telecom",
                    "why": "Public case studies; look for managed-security claims.",
                },
            ],
            "key_questions": [
                "Does CompetitorX offer managed security in the base tier?",
                "What's their typical telecom seat-count cohort?",
                "Any public pricing comparison?",
            ],
            "unfilled_slots": [],
        }),
        (endpoint_id, "email", {
            "subject": "Apex Communications - enterprise telecom proposal",
            "body": (
                "Kevin,\n\nAs promised, the consolidated proposal:\n\n"
                "Pricing: see attached tier sheet (limits {tier_limits}).\n"
                "Managed security: {managed_security_options}.\n"
                "Integration: {integration_feasibility}.\n"
                "Why us over alternatives: {competitor_differentiation}.\n\n"
                "Happy to walk through with your CFO. Let me know what works.\n\nBest,\nMarcus"
            ),
            "cc": [],
            "bcc": [],
            "unfilled_slots": [
                "tier_limits",
                "managed_security_options",
                "integration_feasibility",
                "competitor_differentiation",
            ],
        }),
        (crm_log_id, "system_write_payload", {
            "integration": "hubspot",
            "operation": "update_deal_stage",
            "payload": {
                "deal_external_id": "deal-apex-q3",
                "new_stage": "Proposal Sent",
            },
            "unfilled_slots": [],
        }),
    ]

    for step_id, kind, payload in artifacts:
        db.add(
            StepArtifact(
                step_id=step_id,
                tenant_id=tenant.id,
                version=1,
                kind=kind,
                payload=payload,
                model_tier="sonnet" if step_id == endpoint_id else "haiku",
            )
        )

    # ── One simulated response on the IT step so the UI shows
    # extraction working end-to-end ──
    db.add(
        StepResponse(
            step_id=it_id,
            tenant_id=tenant.id,
            source="manual_note",
            note_text=(
                "IT confirmed integration works out-of-the-box for Apex's "
                "billing system. No custom work required; one-page config doc "
                "available."
            ),
            extracted_data={
                "integration_feasibility": (
                    "Confirmed by IT: integration works out-of-the-box with Apex's "
                    "billing system. One-page config doc available."
                ),
            },
            source_quotes={
                "integration_feasibility": "IT confirmed integration works out-of-the-box",
            },
            unfilled_reasons={},
            extraction_confidence=0.95,
        )
    )

    await db.flush()
    return plan


async def _main(tenant_slug: str) -> None:
    async with async_session() as db:
        tenant = await _resolve_tenant(db, tenant_slug)
        customer = await _upsert_demo_customer(db, tenant)
        interaction = await _upsert_demo_interaction(db, tenant, customer)
        plan = await _seed_plan(db, tenant, customer, interaction)
        await db.commit()
        print(
            json.dumps(
                {
                    "tenant_id": str(tenant.id),
                    "interaction_id": str(interaction.id),
                    "plan_id": str(plan.id),
                    "goal": plan.goal,
                    "steps": len(plan.steps) if plan.steps else 6,
                },
                indent=2,
            )
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-slug", default="demo")
    args = parser.parse_args(argv)
    asyncio.run(_main(args.tenant_slug))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
