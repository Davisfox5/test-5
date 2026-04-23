"""scoring and orchestrator tables

Revision ID: 7a1c3e2d9f40
Revises: 550a40162883
Create Date: 2026-04-17 12:00:00.000000

Adds the feature store (``interaction_features``), the delta-report queue,
and the four versioned profile tables (client/agent/manager/business) used
by the Opus-powered orchestrator.  See ``docs/SCORING_ARCHITECTURE.md``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision: str = "7a1c3e2d9f40"
down_revision: Union[str, None] = "550a40162883"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Users: add manager_id self-FK for manager-profile scoping ──
    op.add_column(
        "users",
        sa.Column("manager_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_manager",
        "users",
        "users",
        ["manager_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── Feature store ──────────────────────────────────────────────
    op.create_table(
        "interaction_features",
        sa.Column("interaction_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "deterministic",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "llm_structured",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("embeddings_ref", sa.Text(), nullable=True),
        sa.Column(
            "proxy_outcomes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "scorer_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("interaction_id"),
    )
    op.create_index(
        "ix_interaction_features_tenant_id",
        "interaction_features",
        ["tenant_id"],
    )

    # ── Delta reports ──────────────────────────────────────────────
    op.create_table(
        "delta_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "delta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delta_reports_tenant_unconsumed",
        "delta_reports",
        ["tenant_id", "consumed_at"],
    )

    # ── Generic profile table factory ──────────────────────────────
    def _profile_table(name: str, entity_fk: str, entity_table: str) -> None:
        op.create_table(
            name,
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column(entity_fk, sa.UUID(), nullable=False),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column(
                "profile",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "top_factors",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "source_event",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint([entity_fk], [f"{entity_table}.id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(entity_fk, "version", name=f"uq_{name}_entity_version"),
        )
        op.create_index(
            f"ix_{name}_entity_latest",
            name,
            [entity_fk, sa.text("version DESC")],
        )

    _profile_table("client_profiles", "contact_id", "contacts")
    _profile_table("agent_profiles", "agent_id", "users")
    _profile_table("manager_profiles", "manager_id", "users")
    _profile_table("business_profiles", "business_tenant_id", "tenants")

    # ── Scorer registry (versions of calibration models) ──────────
    op.create_table(
        "scorer_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),  # nullable = global default
        sa.Column("scorer_name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "calibration",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ece", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "scorer_name", "version", name="uq_scorer_versions_identity"
        ),
    )

    # ── Correction events (active-learning feedback) ──────────────
    op.create_table(
        "correction_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("target_type", sa.String(), nullable=False),  # e.g. "sentiment", "churn_risk"
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column(
            "original",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "correction",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_correction_events_tenant_target",
        "correction_events",
        ["tenant_id", "target_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_correction_events_tenant_target", table_name="correction_events")
    op.drop_table("correction_events")
    op.drop_table("scorer_versions")
    for name, fk in [
        ("business_profiles", "business_tenant_id"),
        ("manager_profiles", "manager_id"),
        ("agent_profiles", "agent_id"),
        ("client_profiles", "contact_id"),
    ]:
        op.drop_index(f"ix_{name}_entity_latest", table_name=name)
        op.drop_table(name)
    op.drop_index("ix_delta_reports_tenant_unconsumed", table_name="delta_reports")
    op.drop_table("delta_reports")
    op.drop_index(
        "ix_interaction_features_tenant_id", table_name="interaction_features"
    )
    op.drop_table("interaction_features")
    op.drop_constraint("fk_users_manager", "users", type_="foreignkey")
    op.drop_column("users", "manager_id")
