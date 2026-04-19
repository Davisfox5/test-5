"""continuous AI improvement: feedback, evaluation, prompt variants, personalisation, ASR vocab, experiments

Revision ID: c0a17e1bf001
Revises: 9b42f1c5e7d3
Create Date: 2026-04-19 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "c0a17e1bf001"
down_revision: Union[str, None] = "9b42f1c5e7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── prompt_variants (no FKs to other new tables — create first) ──────
    op.create_table(
        "prompt_variants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("target_surface", sa.String(), nullable=False),
        sa.Column("target_tier", sa.String(), nullable=True),
        sa.Column("target_channel", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("parent_variant_id", sa.UUID(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prompt_variants_active_lookup",
        "prompt_variants",
        ["target_surface", "status", "target_tier", "target_channel"],
        unique=False,
    )

    # ── interactions / conversations: prompt_variant_id pointer columns ──
    op.add_column("interactions", sa.Column("prompt_variant_id", sa.UUID(), nullable=True))
    op.add_column("conversations", sa.Column("prompt_variant_id", sa.UUID(), nullable=True))

    # ── feedback_events ──────────────────────────────────────────────────
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("action_item_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("signal_type", sa.String(), nullable=False),
        sa.Column("insight_dimension", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_item_id"], ["action_items.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feedback_events_tenant_surface_type",
        "feedback_events",
        ["tenant_id", "surface", "event_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_feedback_events_interaction",
        "feedback_events",
        ["interaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_feedback_events_conversation",
        "feedback_events",
        ["conversation_id"],
        unique=False,
    )

    # ── transcript_corrections ───────────────────────────────────────────
    op.create_table(
        "transcript_corrections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("corrected_text", sa.Text(), nullable=False),
        sa.Column("confidence_at_correction", sa.Float(), nullable=True),
        sa.Column("corrected_by", sa.UUID(), nullable=True),
        sa.Column("correction_source", sa.String(), server_default="manual", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["corrected_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_transcript_corrections_tenant_created",
        "transcript_corrections",
        ["tenant_id", "created_at"],
        unique=False,
    )

    # ── insight_quality_scores ───────────────────────────────────────────
    op.create_table(
        "insight_quality_scores",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("evaluator_type", sa.String(), nullable=False),
        sa.Column("evaluator_id", sa.String(), nullable=False),
        sa.Column("dimension", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("prompt_variant_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_iqs_tenant_surface_dim_created",
        "insight_quality_scores",
        ["tenant_id", "surface", "dimension", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_iqs_interaction",
        "insight_quality_scores",
        ["interaction_id"],
        unique=False,
    )

    # ── tenant_prompt_configs ────────────────────────────────────────────
    op.create_table(
        "tenant_prompt_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("active_prompt_variant_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("few_shot_pool", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("persona_block", sa.Text(), nullable=True),
        sa.Column("acronyms", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("custom_terms", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("rag_config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("parameter_overrides", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )

    # ── vocabulary_candidates ────────────────────────────────────────────
    op.create_table(
        "vocabulary_candidates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("confidence", sa.String(), server_default="medium", nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vocab_candidates_tenant_status",
        "vocabulary_candidates",
        ["tenant_id", "status"],
        unique=False,
    )
    # Same term shouldn't be queued twice for the same tenant while pending.
    op.create_index(
        "uq_vocab_candidates_tenant_term",
        "vocabulary_candidates",
        ["tenant_id", "term"],
        unique=True,
    )

    # ── evaluation_reference_sets ────────────────────────────────────────
    op.create_table(
        "evaluation_reference_sets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("interaction_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("reference_outputs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eval_ref_sets_surface_tenant",
        "evaluation_reference_sets",
        ["surface", "tenant_id"],
        unique=False,
    )

    # ── experiments ──────────────────────────────────────────────────────
    op.create_table(
        "experiments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("surface", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="running", nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("control_variant_id", sa.UUID(), nullable=True),
        sa.Column("treatment_variant_id", sa.UUID(), nullable=True),
        sa.Column("start_date", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_experiments_status_surface",
        "experiments",
        ["status", "surface"],
        unique=False,
    )

    # ── wer_metrics ──────────────────────────────────────────────────────
    op.create_table(
        "wer_metrics",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("asr_engine", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("sample_size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("word_error_rate", sa.Float(), server_default="0", nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wer_metrics_tenant_period",
        "wer_metrics",
        ["tenant_id", "period_end"],
        unique=False,
    )

    # ── cross_tenant_analytics ───────────────────────────────────────────
    # Deliberately no tenant_id column — these aggregates are cross-tenant.
    op.create_table(
        "cross_tenant_analytics",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("metric_name", sa.String(), nullable=False),
        sa.Column("bucket", sa.String(), nullable=True),
        sa.Column("surface", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("sample_size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("distribution", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cta_metric_period",
        "cross_tenant_analytics",
        ["metric_name", "period_end"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cta_metric_period", table_name="cross_tenant_analytics")
    op.drop_table("cross_tenant_analytics")

    op.drop_index("ix_wer_metrics_tenant_period", table_name="wer_metrics")
    op.drop_table("wer_metrics")

    op.drop_index("ix_experiments_status_surface", table_name="experiments")
    op.drop_table("experiments")

    op.drop_index("ix_eval_ref_sets_surface_tenant", table_name="evaluation_reference_sets")
    op.drop_table("evaluation_reference_sets")

    op.drop_index("uq_vocab_candidates_tenant_term", table_name="vocabulary_candidates")
    op.drop_index("ix_vocab_candidates_tenant_status", table_name="vocabulary_candidates")
    op.drop_table("vocabulary_candidates")

    op.drop_table("tenant_prompt_configs")

    op.drop_index("ix_iqs_interaction", table_name="insight_quality_scores")
    op.drop_index("ix_iqs_tenant_surface_dim_created", table_name="insight_quality_scores")
    op.drop_table("insight_quality_scores")

    op.drop_index("ix_transcript_corrections_tenant_created", table_name="transcript_corrections")
    op.drop_table("transcript_corrections")

    op.drop_index("ix_feedback_events_conversation", table_name="feedback_events")
    op.drop_index("ix_feedback_events_interaction", table_name="feedback_events")
    op.drop_index("ix_feedback_events_tenant_surface_type", table_name="feedback_events")
    op.drop_table("feedback_events")

    op.drop_column("conversations", "prompt_variant_id")
    op.drop_column("interactions", "prompt_variant_id")

    op.drop_index("ix_prompt_variants_active_lookup", table_name="prompt_variants")
    op.drop_table("prompt_variants")
