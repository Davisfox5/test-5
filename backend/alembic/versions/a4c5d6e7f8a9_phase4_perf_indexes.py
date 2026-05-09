"""Phase 4 — performance indexes for hot tenant-scoped reads.

The cost-audit identified three tables that drove most slow-query latency:

1. ``interactions`` — every dashboard, analytics, and search endpoint
   filters by ``(tenant_id, created_at)``. Today only ``customer_id`` is
   indexed; tenant scopes fall through to seq scan or loose index range.
   We add: ``(tenant_id, created_at)`` composite + single-column
   ``tenant_id``, ``agent_id``, ``contact_id``.

2. ``customers`` — list paging orders by name within tenant. Add
   ``(tenant_id, name)`` composite + single-column ``tenant_id`` to
   support IN/JOIN filters from ``customer_owners``.

3. ``email_sends`` — audit list reads order by ``created_at`` within
   tenant. Add the missing composite (single columns already exist
   for ``tenant_id`` and ``interaction_id``).

All indexes are created ``CONCURRENTLY`` so a production rollout doesn't
take an exclusive lock on multi-million-row tables. ``transactional_ddl
= False`` is required because PostgreSQL refuses
``CREATE INDEX CONCURRENTLY`` inside a transaction block.

Mirrors are added to ``models.py`` ``__table_args__`` so ``create_all``
on a fresh DB produces the same indexes.

Revision ID: a4c5d6e7f8a9
Revises: z3b4c5d6e7f8
Create Date: 2026-05-09
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "a4c5d6e7f8a9"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CONCURRENTLY needs to run outside any transaction.
transactional_ddl = False


def upgrade() -> None:
    op.execute("COMMIT")  # close the implicit migration transaction
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_interaction_tenant_created "
        "ON interactions (tenant_id, created_at)"
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_interaction_tenant_id "
        "ON interactions (tenant_id)"
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_interaction_agent_id "
        "ON interactions (agent_id)"
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_interaction_contact_id "
        "ON interactions (contact_id)"
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_customer_tenant_id "
        "ON customers (tenant_id)"
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_customer_tenant_name "
        "ON customers (tenant_id, name)"
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_email_send_tenant_created "
        "ON email_sends (tenant_id, created_at)"
    )


def downgrade() -> None:
    op.execute("COMMIT")
    for ix in (
        "ix_email_send_tenant_created",
        "ix_customer_tenant_name",
        "ix_customer_tenant_id",
        "ix_interaction_contact_id",
        "ix_interaction_agent_id",
        "ix_interaction_tenant_id",
        "ix_interaction_tenant_created",
    ):
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {ix}")
