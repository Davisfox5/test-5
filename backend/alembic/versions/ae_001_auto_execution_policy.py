"""Governed auto-executor: AutoExecutionPolicy + new state/source literals.

Adds ``auto_execution_policies`` — the per-tenant, per-action-class
dispatch policy the executor (``services/action_plan/executor.py``)
consults before touching a ready step. Keyed by (tenant_id,
action_class); no row for a class means ``manual`` (today's behavior,
nothing auto-dispatches). This is a NEW tenant-scoped table, so it gets
its own RLS policy here — see ``rls_002_all_tables``'s docstring: "A
TABLE ADDED AFTER THIS MIGRATION GETS NO POLICIES until its own policy
migration ships."

Also widens two existing CHECK constraints (plain string columns, not DB
enum types, but still vocab-constrained at the DB level) to admit the
literals the executor introduces:

* ``action_steps.state`` gains ``'pending_approval'`` — set when a
  tenant's policy is ``approve_then_auto``; a human approves elsewhere.
* ``step_responses.source`` gains ``'auto_executed'`` — the audit row
  the executor writes for every real or shadow dispatch.

Revision ID: ae_001_auto_execution_policy
Revises: rls_002_all_tables
Create Date: 2026-07-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "ae_001_auto_execution_policy"
down_revision: Union[str, None] = "rls_002_all_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTION_CLASS_VOCAB = "('low_risk', 'high_risk')"
_POLICY_MODE_VOCAB = "('manual', 'shadow', 'approve_then_auto', 'auto')"

_STEP_STATE_VOCAB_OLD = (
    "('blocked', 'ready', 'in_progress', 'awaiting_response', 'done', "
    "'skipped', 'deleted')"
)
_STEP_STATE_VOCAB_NEW = (
    "('blocked', 'ready', 'in_progress', 'awaiting_response', 'done', "
    "'skipped', 'deleted', 'pending_approval')"
)
_RESPONSE_SOURCE_VOCAB_OLD = (
    "('inbound_email', 'manual_note', 'auto_mark_done', 'outbound_email_sent')"
)
_RESPONSE_SOURCE_VOCAB_NEW = (
    "('inbound_email', 'manual_note', 'auto_mark_done', 'outbound_email_sent', "
    "'auto_executed')"
)


def upgrade() -> None:
    op.create_table(
        "auto_execution_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_class", sa.String(length=32), nullable=False),
        sa.Column(
            "mode", sa.String(length=24), nullable=False, server_default="manual",
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.UniqueConstraint(
            "tenant_id", "action_class", name="uq_auto_execution_policy_class"
        ),
        sa.CheckConstraint(
            f"action_class IN {_ACTION_CLASS_VOCAB}",
            name="ck_auto_execution_policy_action_class",
        ),
        sa.CheckConstraint(
            f"mode IN {_POLICY_MODE_VOCAB}",
            name="ck_auto_execution_policy_mode",
        ),
    )
    op.create_index(
        "ix_auto_execution_policies_tenant_id",
        "auto_execution_policies",
        ["tenant_id"],
    )

    op.drop_constraint("ck_action_steps_state", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_state", "action_steps", f"state IN {_STEP_STATE_VOCAB_NEW}",
    )
    op.drop_constraint("ck_step_responses_source", "step_responses", type_="check")
    op.create_check_constraint(
        "ck_step_responses_source",
        "step_responses",
        f"source IN {_RESPONSE_SOURCE_VOCAB_NEW}",
    )

    # RLS for the new table — the new-table checklist in
    # tests/test_rls_scoping_guard.py + rls_002_all_tables's docstring.
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    import os

    from backend.app import rls

    for stmt in rls.rls_statements(tables=["auto_execution_policies"]):
        conn.execute(sa.text(stmt))

    role = os.environ.get("APP_DB_ROLE", "linda_app")
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar()
    if exists:
        conn.execute(
            sa.text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON auto_execution_policies "
                "TO {r}".format(r=role)
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        for policy in (
            "tenant_isolation_select",
            "tenant_isolation_insert",
            "tenant_isolation_update",
            "tenant_isolation_delete",
        ):
            conn.execute(
                sa.text(
                    "DROP POLICY IF EXISTS {p} ON auto_execution_policies".format(
                        p=policy
                    )
                )
            )
        conn.execute(
            sa.text(
                "ALTER TABLE auto_execution_policies DISABLE ROW LEVEL SECURITY"
            )
        )

    op.drop_constraint("ck_step_responses_source", "step_responses", type_="check")
    op.create_check_constraint(
        "ck_step_responses_source",
        "step_responses",
        f"source IN {_RESPONSE_SOURCE_VOCAB_OLD}",
    )
    op.drop_constraint("ck_action_steps_state", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_state", "action_steps", f"state IN {_STEP_STATE_VOCAB_OLD}",
    )

    op.drop_index(
        "ix_auto_execution_policies_tenant_id", table_name="auto_execution_policies"
    )
    op.drop_table("auto_execution_policies")
