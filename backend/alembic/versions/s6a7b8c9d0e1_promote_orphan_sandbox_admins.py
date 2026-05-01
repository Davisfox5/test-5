"""Promote sole-user-of-sandbox tenants to admin (fix orphan signup users).

A bug in ``/api/v1/trial/signup`` — its short-circuit branch returns
the existing tenant for users whose Clerk identity was lazy-created
at /me bootstrap time *before* they completed the proper trial-signup
flow. The lazy-create path defaults ``users.role`` to ``"agent"``
because it doesn't know whether the user is the first user of the
tenant. The short-circuit then never re-promotes.

Net effect: users who clicked the wrong sign-up URL (Clerk-only
``/sign-up``) before going through the trial flow stayed locked at
agent role, with no admin in the tenant — they couldn't reach
Settings, generate API keys, or invite teammates.

This data migration scans for that pattern (sole active user, role
agent, tenant on the sandbox tier) and flips them to admin. It is
narrowly scoped — multi-user tenants and any tenant beyond sandbox
are untouched, since flipping a non-sole-admin user there could
unintentionally elevate someone in a real customer org.

Revision ID: s6a7b8c9d0e1
Revises: r5f6a7b8c9d0
Create Date: 2026-05-01

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "s6a7b8c9d0e1"
down_revision = "r5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            UPDATE users
            SET role = 'admin'
            WHERE id IN (
                SELECT u.id
                FROM users u
                JOIN tenants t ON t.id = u.tenant_id
                WHERE u.role = 'agent'
                  AND u.is_active IS TRUE
                  AND t.plan_tier = 'sandbox'
                  AND (
                      SELECT COUNT(*)
                      FROM users u2
                      WHERE u2.tenant_id = u.tenant_id
                        AND u2.is_active IS TRUE
                  ) = 1
            )
            """
        )
    )


def downgrade() -> None:
    # No-op. Demoting back to agent risks data loss (admin-created
    # records, API keys, etc.); the upgrade is forward-only.
    pass
