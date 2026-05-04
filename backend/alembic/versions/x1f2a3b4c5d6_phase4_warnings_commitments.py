"""Phase 4 — customer_warnings + commitments tables.

The plan calls these the "differentiation layer": named, explainable
findings on a customer's record (Gong-style) plus both-sides promises
extracted from transcripts. Distinct from action items, which remain
rep-side TODOs only.

``customer_warnings`` — replaces the opaque numeric risk score with a
list of finite-vocabulary findings. ``kind`` is pinned by CHECK to the
warning vocabulary in section 5 of the plan; ``severity`` is low/med/
high. Warnings can be dismissed by a user; the dismissal is sticky
unless the underlying signal recurs in a later interaction (the
warnings engine re-raises by clearing ``dismissed_at`` when the next
pipeline run on the same customer detects the same kind again).

``commitments`` — extracted from transcripts on both sides.
``actor_user_id`` and ``actor_contact_id`` are mutually exclusive (one
populated, one NULL); same shape on the target side. ``status`` flows
pending → done | overdue | dismissed; done detection mirrors action
items (manual confirm + LLM scan of subsequent calls + integration
events when wired). ``due_date`` is anchored to the originating
interaction's ``created_at`` at extraction time, so phrases like
"by Friday" remain meaningful even if a user views the record weeks
later.

Revision ID: x1f2a3b4c5d6
Revises: w0e1f2a3b4c5
Create Date: 2026-05-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "x1f2a3b4c5d6"
down_revision: Union[str, None] = "w0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_WARNING_KINDS = (
    "single_threaded",
    "champion_silent",
    "competitor_mentioned",
    "no_next_step",
    "exec_disengaged",
    "pricing_unapproved",
    "stalled_renewal",
    "negative_sentiment_trend",
    "other",
)


_COMMITMENT_STATUSES = ("pending", "done", "overdue", "dismissed")


def upgrade() -> None:
    op.create_table(
        "customer_warnings",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=True),
        sa.Column(
            "evidence_interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "first_detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "dismissed_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind IN ({})".format(
                ", ".join(f"'{k}'" for k in _WARNING_KINDS)
            ),
            name="ck_customer_warnings_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')",
            name="ck_customer_warnings_severity",
        ),
        # One row per (customer, kind) — re-detection updates the
        # existing row (clears dismissed_at, bumps last_detected_at,
        # refreshes evidence) rather than spawning duplicates.
        sa.UniqueConstraint(
            "customer_id", "kind", name="uq_customer_warnings_customer_kind"
        ),
    )

    op.create_table(
        "commitments",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # actor_user_id XOR actor_contact_id; the side that promised.
        sa.Column(
            "actor_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_contact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # target_user_id XOR target_contact_id; who the promise is to.
        # Both can be NULL when the commitment is general ("we'll send
        # the proposal").
        sa.Column(
            "target_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_contact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("evidence_excerpt", sa.Text(), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "actor_side",
            sa.String(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "completed_via", sa.String(), nullable=True
        ),
        sa.Column(
            "completed_evidence_interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ({})".format(
                ", ".join(f"'{s}'" for s in _COMMITMENT_STATUSES)
            ),
            name="ck_commitments_status",
        ),
        sa.CheckConstraint(
            "actor_side IN ('rep', 'customer', 'unknown')",
            name="ck_commitments_actor_side",
        ),
        # Exactly-one actor identity (User XOR Contact, both allowed
        # NULL only when the LLM couldn't pin the speaker — actor_side
        # still records which side made the promise).
        sa.CheckConstraint(
            "(actor_user_id IS NULL) OR (actor_contact_id IS NULL)",
            name="ck_commitments_actor_xor",
        ),
        sa.CheckConstraint(
            "(target_user_id IS NULL) OR (target_contact_id IS NULL)",
            name="ck_commitments_target_xor",
        ),
    )


def downgrade() -> None:
    op.drop_table("commitments")
    op.drop_table("customer_warnings")
