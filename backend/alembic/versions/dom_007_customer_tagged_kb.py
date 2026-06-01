"""Customer-tagged KB: per-document customer scoping.

Adds ``customer_id`` to ``kb_documents`` and (denormalized) to
``kb_chunks`` so KB retrieval can filter to "general + this customer"
in a single index lookup instead of a join.

Auto-tagged at ingest:

* Documents the AI produces that anchor to a customer (action-plan
  artifacts, follow-up emails) inherit the ``customer_id`` from the
  source interaction.
* Documents uploaded manually by an agent get a customer picker in
  the UI; default null = visible to every agent in the tenant.

NULL means "general document — applies to every customer." The
retrieval filter is ``WHERE customer_id IS NULL OR customer_id = ?``
so a customer-specific doc augments the general KB, never replaces it.

Revision ID: dom_007_customer_tagged_kb
Revises: dom_006_customer_memory
Create Date: 2026-06-01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "dom_007_customer_tagged_kb"
down_revision: Union[str, None] = "dom_006_customer_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_t = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)

    op.add_column(
        "kb_documents",
        sa.Column(
            "customer_id",
            uuid_t,
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_kb_documents_tenant_customer",
        "kb_documents",
        ["tenant_id", "customer_id"],
    )

    op.add_column(
        "kb_chunks",
        sa.Column(
            "customer_id",
            uuid_t,
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_kb_chunks_tenant_customer",
        "kb_chunks",
        ["tenant_id", "customer_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kb_chunks_tenant_customer", table_name="kb_chunks")
    op.drop_column("kb_chunks", "customer_id")
    op.drop_index(
        "ix_kb_documents_tenant_customer", table_name="kb_documents"
    )
    op.drop_column("kb_documents", "customer_id")
