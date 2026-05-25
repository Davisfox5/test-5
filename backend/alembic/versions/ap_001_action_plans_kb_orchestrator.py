"""Action Plans + KB orchestrator typed chunks + per-team domain default.

Introduces the DAG-based Action Plan successor to ActionItem:

* ``action_plans`` — the workflow envelope (one per interaction, or
  free-standing for manual plans).
* ``action_steps`` — DAG nodes with depends_on, input_slots,
  output_schema, output_data, state machine, KB grounding, integration
  target, artifact freshness fields.
* ``step_artifacts`` — versioned, append-only drafts produced by Call C.
  The latest version per step is the active artifact; older rows let
  the UI diff "what I had vs the regenerated draft."
* ``step_responses`` — inbound emails, manual notes, or auto-mark-done
  events that fulfill a step. Extraction (Call D) writes its structured
  output here; the engine flows the values into downstream input_slots.
* ``kb_integration_gaps`` — procedures whose required_integrations
  reference unconnected providers. Drives the admin KB-integration
  alignment report and gets re-evaluated when integrations change.

Extends ``kb_chunks`` with Document Orchestrator output: ``kind``
(procedure | policy | escalation_path | template | context | faq |
glossary | contact_directory), ``extracted_metadata`` JSONB,
``classification_confidence``, and source_span offsets.

Adds ``tenants.default_domain`` (NOT NULL, default 'generic') and
``users.default_domain`` (nullable — inherits tenant default).

ActionItem (and the action_items table) is left in place during the
cutover so the existing pipeline + Linda agent + API endpoints
continue to work. Removal lands in a later migration once consumers
have moved to action_plans / action_steps.

Revision ID: ap_001_action_plans_kb_orchestrator
Revises: tnt_001_role_preview_enabled
Create Date: 2026-05-25
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "ap_001_action_plans_kb_orchestrator"
down_revision: Union[str, None] = "tnt_001_role_preview_enabled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DOMAIN_VOCAB = "('sales', 'customer_service', 'it_support', 'generic')"
_CHUNK_KIND_VOCAB = (
    "('procedure', 'policy', 'escalation_path', 'template', 'context', "
    "'faq', 'glossary', 'contact_directory')"
)
_STEP_STATE_VOCAB = (
    "('blocked', 'ready', 'in_progress', 'awaiting_response', 'done', "
    "'skipped', 'deleted')"
)
_STEP_ROLE_VOCAB = "('preparation', 'customer_endpoint', 'post_completion')"
_COMPLIANCE_VOCAB = "('must', 'should', 'may')"
_RESPONSE_SOURCE_VOCAB = (
    "('inbound_email', 'manual_note', 'auto_mark_done', 'outbound_email_sent')"
)
_PLAN_STATUS_VOCAB = "('draft', 'active', 'completed', 'abandoned')"


def upgrade() -> None:
    # ── tenants / users: default_domain ──
    op.add_column(
        "tenants",
        sa.Column(
            "default_domain",
            sa.String(),
            nullable=False,
            server_default="generic",
        ),
    )
    op.create_check_constraint(
        "ck_tenants_default_domain",
        "tenants",
        f"default_domain IN {_DOMAIN_VOCAB}",
    )

    op.add_column(
        "users",
        sa.Column("default_domain", sa.String(), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_default_domain",
        "users",
        f"default_domain IS NULL OR default_domain IN {_DOMAIN_VOCAB}",
    )

    # ── kb_chunks: orchestrator output ──
    op.add_column(
        "kb_chunks",
        sa.Column(
            "kind",
            sa.String(),
            nullable=False,
            server_default="context",
        ),
    )
    op.add_column(
        "kb_chunks",
        sa.Column(
            "extracted_metadata",
            JSONB,
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "kb_chunks",
        sa.Column("classification_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "kb_chunks",
        sa.Column("source_span_start", sa.Integer(), nullable=True),
    )
    op.add_column(
        "kb_chunks",
        sa.Column("source_span_end", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_kb_chunks_kind",
        "kb_chunks",
        f"kind IN {_CHUNK_KIND_VOCAB}",
    )
    op.create_index(
        "ix_kb_chunks_tenant_kind",
        "kb_chunks",
        ["tenant_id", "kind"],
    )

    # ── action_plans ──
    op.create_table(
        "action_plans",
        sa.Column(
            "id", UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interaction_id", UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=True, unique=True,
        ),
        sa.Column(
            "customer_id", UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("goal", sa.String(), nullable=True),
        sa.Column(
            "domain", sa.String(),
            nullable=False, server_default="generic",
        ),
        sa.Column(
            "status", sa.String(),
            nullable=False, server_default="active",
        ),
        sa.Column(
            "customer_endpoint_step_id", UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "procedures_applied", JSONB,
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "external_context_snapshot", JSONB,
            nullable=False, server_default="{}",
        ),
        sa.Column(
            "version", sa.Integer(),
            nullable=False, server_default="1",
        ),
        sa.Column(
            "manually_created", sa.Boolean(),
            nullable=False, server_default="false",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            f"domain IN {_DOMAIN_VOCAB}",
            name="ck_action_plans_domain",
        ),
        sa.CheckConstraint(
            f"status IN {_PLAN_STATUS_VOCAB}",
            name="ck_action_plans_status",
        ),
    )
    op.create_index(
        "ix_action_plans_tenant_status", "action_plans",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_action_plans_customer_id", "action_plans",
        ["customer_id"],
    )

    # ── action_steps ──
    op.create_table(
        "action_steps",
        sa.Column(
            "id", UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "plan_id", UUID(as_uuid=True),
            sa.ForeignKey("action_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assigned_to", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column(
            "priority", sa.String(),
            nullable=False, server_default="medium",
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("recommended_channel", sa.String(32), nullable=True),
        sa.Column("channel_reasoning", sa.Text(), nullable=True),
        sa.Column(
            "participants", JSONB,
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "prep_artifacts", JSONB,
            nullable=False, server_default="[]",
        ),
        sa.Column("implicit_signal", sa.Text(), nullable=True),
        sa.Column(
            "state", sa.String(),
            nullable=False, server_default="ready",
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "skipped_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "depends_on", JSONB,
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "input_slots", JSONB,
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "output_schema", JSONB,
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "output_data", JSONB,
            nullable=False, server_default="{}",
        ),
        sa.Column("kb_source", JSONB, nullable=True),
        sa.Column("compliance_level", sa.String(8), nullable=True),
        sa.Column(
            "role_in_plan", sa.String(20),
            nullable=False, server_default="preparation",
        ),
        sa.Column("target_integration", sa.String(32), nullable=True),
        sa.Column("integration_operation", sa.String(64), nullable=True),
        sa.Column(
            "artifact_version", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "artifact_stale", sa.Boolean(),
            nullable=False, server_default="false",
        ),
        sa.Column(
            "regen_debounce_until", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "feedback_score", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column("calendar_event_id", sa.String(), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"state IN {_STEP_STATE_VOCAB}",
            name="ck_action_steps_state",
        ),
        sa.CheckConstraint(
            f"role_in_plan IN {_STEP_ROLE_VOCAB}",
            name="ck_action_steps_role_in_plan",
        ),
        sa.CheckConstraint(
            f"compliance_level IS NULL OR compliance_level IN {_COMPLIANCE_VOCAB}",
            name="ck_action_steps_compliance_level",
        ),
    )
    op.create_index(
        "ix_action_steps_plan_state", "action_steps",
        ["plan_id", "state"],
    )
    op.create_index(
        "ix_action_steps_tenant_assigned", "action_steps",
        ["tenant_id", "assigned_to"],
    )
    op.create_index(
        "ix_action_steps_regen_due", "action_steps",
        ["regen_debounce_until"],
    )

    # ── step_artifacts ──
    op.create_table(
        "step_artifacts",
        sa.Column(
            "id", UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "step_id", UUID(as_uuid=True),
            sa.ForeignKey("action_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "payload", JSONB,
            nullable=False, server_default="{}",
        ),
        sa.Column("model_tier", sa.String(16), nullable=True),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "superseded_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "step_id", "version",
            name="uq_step_artifact_version",
        ),
    )
    op.create_index(
        "ix_step_artifacts_step_version", "step_artifacts",
        ["step_id", "version"],
    )

    # ── step_responses ──
    op.create_table(
        "step_responses",
        sa.Column(
            "id", UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "step_id", UUID(as_uuid=True),
            sa.ForeignKey("action_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("email_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("outbound_message_id", sa.String(), nullable=True),
        sa.Column("note_text", sa.Text(), nullable=True),
        sa.Column(
            "extracted_data", JSONB,
            nullable=False, server_default="{}",
        ),
        sa.Column(
            "unfilled_reasons", JSONB,
            nullable=False, server_default="{}",
        ),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column(
            "source_quotes", JSONB,
            nullable=False, server_default="{}",
        ),
        sa.Column(
            "received_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "agent_overridden", sa.Boolean(),
            nullable=False, server_default="false",
        ),
        sa.CheckConstraint(
            f"source IN {_RESPONSE_SOURCE_VOCAB}",
            name="ck_step_responses_source",
        ),
    )
    op.create_index(
        "ix_step_responses_step_received", "step_responses",
        ["step_id", "received_at"],
    )
    op.create_index(
        "ix_step_responses_outbound_msg", "step_responses",
        ["outbound_message_id"],
    )

    # ── kb_integration_gaps ──
    op.create_table(
        "kb_integration_gaps",
        sa.Column(
            "id", UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id", UUID(as_uuid=True),
            sa.ForeignKey("kb_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "doc_id", UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("procedure_title", sa.String(), nullable=True),
        sa.Column("required_provider", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(64), nullable=True),
        sa.Column(
            "compliance_level", sa.String(8),
            nullable=False, server_default="should",
        ),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "chunk_id", "required_provider", "operation",
            name="uq_kb_integration_gap",
        ),
    )
    op.create_index(
        "ix_kb_integration_gaps_tenant_provider", "kb_integration_gaps",
        ["tenant_id", "required_provider"],
    )


def downgrade() -> None:
    # New-only tables — drop in reverse FK order.
    op.drop_index(
        "ix_kb_integration_gaps_tenant_provider",
        table_name="kb_integration_gaps",
    )
    op.drop_table("kb_integration_gaps")

    op.drop_index(
        "ix_step_responses_outbound_msg", table_name="step_responses",
    )
    op.drop_index(
        "ix_step_responses_step_received", table_name="step_responses",
    )
    op.drop_table("step_responses")

    op.drop_index(
        "ix_step_artifacts_step_version", table_name="step_artifacts",
    )
    op.drop_table("step_artifacts")

    op.drop_index(
        "ix_action_steps_regen_due", table_name="action_steps",
    )
    op.drop_index(
        "ix_action_steps_tenant_assigned", table_name="action_steps",
    )
    op.drop_index(
        "ix_action_steps_plan_state", table_name="action_steps",
    )
    op.drop_table("action_steps")

    op.drop_index(
        "ix_action_plans_customer_id", table_name="action_plans",
    )
    op.drop_index(
        "ix_action_plans_tenant_status", table_name="action_plans",
    )
    op.drop_table("action_plans")

    op.drop_index("ix_kb_chunks_tenant_kind", table_name="kb_chunks")
    op.drop_constraint("ck_kb_chunks_kind", "kb_chunks", type_="check")
    op.drop_column("kb_chunks", "source_span_end")
    op.drop_column("kb_chunks", "source_span_start")
    op.drop_column("kb_chunks", "classification_confidence")
    op.drop_column("kb_chunks", "extracted_metadata")
    op.drop_column("kb_chunks", "kind")

    op.drop_constraint("ck_users_default_domain", "users", type_="check")
    op.drop_column("users", "default_domain")

    op.drop_constraint("ck_tenants_default_domain", "tenants", type_="check")
    op.drop_column("tenants", "default_domain")
