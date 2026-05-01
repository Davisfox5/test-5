"""Add ``preview_role`` to ``users`` for the sandbox role-preview switcher.

Sandbox-tier tenants get a read-only role override so a single trial
user can preview agent / manager / admin views without seeding extra
test users. The override is render-time UX only — the user's real
``role`` column remains the source of truth for security. The principal
resolver in :mod:`backend.app.auth` applies the override only when the
tenant is on sandbox AND the trial is still active (see the security
model in the PR description for the full three-layer gate).

Schema notes:

* Nullable text — NULL means "no override, use the real role".
* CHECK constraint pins the value to the same role-name vocabulary the
  rest of the codebase uses (``agent`` / ``manager`` / ``admin``). A
  stray ``'owner'`` from an earlier draft of this feature would never
  have rendered correctly anyway; better to fail loud at the DB.
* No backfill — the column starts NULL for every existing user, which
  is the correct "no preview" state.

Revision ID: t7b8c9d0e1f2
Revises: s6a7b8c9d0e1
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "t7b8c9d0e1f2"
down_revision: Union[str, None] = "s6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preview_role", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_preview_role",
        "users",
        "preview_role IS NULL OR preview_role IN ('agent', 'manager', 'admin')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_preview_role", "users", type_="check")
    op.drop_column("users", "preview_role")
