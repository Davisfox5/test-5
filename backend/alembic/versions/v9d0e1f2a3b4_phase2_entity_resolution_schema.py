"""Phase 2 entity-resolution schema additions.

The customer-centric redesign needs:

1. ``interactions.customer_id`` — direct FK to customers, populated by the
   new entity-resolution step in ``_run_pipeline_impl``. Today the link
   is only via ``contact_id``, which strands interactions where no
   specific contact was identified (cold outbound, multi-party calls,
   inferred-org-but-no-name).

2. ``contacts.role`` + ``role_confidence`` — auto-inferred contact role
   in the buying group (champion / economic_buyer / user / blocker /
   coach). Same confidence-tier UX as customer auto-creation: solid chip
   ≥80%, suggested chip 60–80%, absent <60%. CHECK constraint pins the
   vocabulary; user-editable in the contact detail page.

3. ``customers.parent_customer_id`` — self-FK for enterprise hierarchy
   (Acme → Acme Logistics, Acme Cloud). No editing UI in v1; populated
   from CRM sync when present.

4. ``customers.timezone`` — drives meeting scheduling later. Auto-default
   from CRM HQ or contact email-signature TZ; editable.

5. ``customers.strongest_connection_user_id`` — denormalized result of a
   nightly job that picks the Linda user with the most call airtime +
   email volume on this customer's interactions over the trailing 90 days.

6. ``customer_owners`` — many-to-many between customers and users. One
   primary owner; subsequent calls with a different rep auto-add that
   rep as secondary. Replaces the implicit "first interaction's
   uploader is the owner" pattern with an explicit, queryable model.

Revision ID: v9d0e1f2a3b4
Revises: u8c9d0e1f2a3
Create Date: 2026-05-03

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "v9d0e1f2a3b4"
down_revision: Union[str, None] = "u8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. interactions.customer_id
    op.add_column(
        "interactions",
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_interactions_customer_id",
        "interactions",
        ["customer_id"],
    )

    # 2. contacts.role + role_confidence
    op.add_column("contacts", sa.Column("role", sa.String(), nullable=True))
    op.add_column(
        "contacts",
        sa.Column("role_confidence", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "ck_contacts_role",
        "contacts",
        "role IS NULL OR role IN ('champion', 'economic_buyer', 'user', 'blocker', 'coach')",
    )

    # 3. customers.parent_customer_id (self-FK)
    op.add_column(
        "customers",
        sa.Column(
            "parent_customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_customers_parent_customer_id",
        "customers",
        ["parent_customer_id"],
    )

    # 4. customers.timezone
    op.add_column(
        "customers",
        sa.Column("timezone", sa.String(), nullable=True),
    )

    # 5. customers.strongest_connection_user_id
    op.add_column(
        "customers",
        sa.Column(
            "strongest_connection_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # 6. customer_owners
    op.create_table(
        "customer_owners",
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
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("assigned_via", sa.String(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "customer_id", "user_id", name="uq_customer_owners_customer_user"
        ),
        sa.CheckConstraint(
            "role IN ('primary', 'secondary')",
            name="ck_customer_owners_role",
        ),
        sa.CheckConstraint(
            "assigned_via IN ('first_uploader', 'speaker_tag', 'manual')",
            name="ck_customer_owners_assigned_via",
        ),
    )


def downgrade() -> None:
    op.drop_table("customer_owners")
    op.drop_column("customers", "strongest_connection_user_id")
    op.drop_column("customers", "timezone")
    op.drop_index("ix_customers_parent_customer_id", table_name="customers")
    op.drop_column("customers", "parent_customer_id")
    op.drop_constraint("ck_contacts_role", "contacts", type_="check")
    op.drop_column("contacts", "role_confidence")
    op.drop_column("contacts", "role")
    op.drop_index("ix_interactions_customer_id", table_name="interactions")
    op.drop_column("interactions", "customer_id")
