"""Canonicalize ``users.role`` to {agent, manager, admin}.

The role vocabulary has always been agent/manager/admin in code (see
``_ROLE_RANK`` in :mod:`backend.app.auth` and ``role >= admin`` gates
across ``/api/v1/admin/*``), but the column itself was free-form
``String`` with no DB-side constraint. At least one production row
ended up with ``role = 'executive'`` — most likely from an early draft
of the role hierarchy that never got migrated. The downstream effect is
silent: the auth resolver returns ``"executive"`` raw, the role-rank
lookup falls through to ``0``, and the user is treated as below-agent
on every gate. The SPA's sidebar mirrors this with its
``normalizeRole`` fallback to ``"agent"``, which is why the affected
user sees an Agent label even though the header pill renders the raw
``executive`` value. Three different surfaces, three different answers
to "what is my role" — but all rooted in this one rogue DB value.

This migration:

1. Promotes any ``role`` value not in the canonical set to ``'admin'``.
   ``'executive'`` is the only known case in the wild, but the WHERE
   clause is defensive: anything outside the set gets normalised the
   same way. ``'admin'`` is the safer of the two endpoints because
   these users are already past every authentication check and we
   don't want to demote a real admin to agent through a data-clean
   migration.
2. Installs a CHECK constraint that pins the column to the canonical
   vocabulary going forward. NULL is permitted (matches the auth
   resolver's ``user.role or "agent"`` fallback for legacy rows).

Revision ID: u8c9d0e1f2a3
Revises: t7b8c9d0e1f2
Create Date: 2026-05-03

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "u8c9d0e1f2a3"
down_revision: Union[str, None] = "t7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CANONICAL_ROLES = ("agent", "manager", "admin")


def upgrade() -> None:
    op.execute(
        text(
            """
            UPDATE users
            SET role = 'admin'
            WHERE role IS NOT NULL
              AND role NOT IN ('agent', 'manager', 'admin')
            """
        )
    )
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IS NULL OR role IN ('agent', 'manager', 'admin')",
    )


def downgrade() -> None:
    # Forward-only on the data side: we don't know which rows used to
    # be 'executive' (or anything else) so we can't restore them.
    # Drop the constraint so future writes accept any string again.
    op.drop_constraint("ck_users_role", "users", type_="check")
