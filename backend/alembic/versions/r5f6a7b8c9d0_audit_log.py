"""Add the comprehensive ``audit_log`` table.

The pre-existing :class:`~backend.app.models.TenantDataOpsLog` only
covers GDPR data-ops (export, hard-delete). This adds a generic audit
log that every mutating endpoint can write to via the
``backend.app.services.audit_log.audit_log`` helper.

Schema notes:

* ``actor_user_id`` is nullable for API-key actors — they don't have a
  ``users`` row.
* ``actor_principal`` records what kind of credential was used
  (``user`` / ``api_key`` / ``system``).
* ``before`` / ``after`` are JSONB snapshots so a security review can
  diff a user's role change without joining back to the live row (which
  may have been mutated again or deleted).
* ``metadata`` carries the request_id / IP / user-agent so we can
  correlate with the request log on incident review. Named
  ``metadata_`` in the model because SQLAlchemy reserves ``metadata``
  on declarative bases; the column is just ``metadata`` in SQL.
* The composite index ``(tenant_id, created_at desc)`` matches the
  admin list query exactly — pagination does ``WHERE tenant_id = ?
  ORDER BY created_at DESC LIMIT ?``.

Revision ID: r5f6a7b8c9d0
Revises: q4e5f6a7b8c9
Create Date: 2026-04-28 02:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "r5f6a7b8c9d0"
down_revision: Union[str, None] = "q4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Stored as a bare UUID rather than a foreign key so audit rows
        # outlive the tenant they describe (matches TenantDataOpsLog).
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_principal", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String()),
        sa.Column("before", postgresql.JSONB()),
        sa.Column("after", postgresql.JSONB()),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_log_tenant_created",
        "audit_log",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_audit_log_action",
        "audit_log",
        ["action"],
    )
    op.create_index(
        "ix_audit_log_resource_type",
        "audit_log",
        ["resource_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_resource_type", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")
