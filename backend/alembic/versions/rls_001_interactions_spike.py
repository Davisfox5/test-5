"""Tenant RLS spike: interactions policies + tenant bootstrap + app-role grants.

First increment of the isolation backstop
(docs/complexity/04-tenant-isolation-migration.md §7): enable row-level
security on ``interactions``, install the SECURITY DEFINER function Celery
uses to resolve an interaction's tenant before it can read anything, and —
when the non-owner ``linda_app`` role already exists — grant it schema
access. The owner connection Alembic runs on bypasses the policies by
definition (RLS is not FORCEd), so this migration can never lock itself out.

Role creation is deliberately NOT done here: login roles need passwords and
those don't belong in a migration. Create the role first (Neon console or
``CREATE ROLE linda_app LOGIN PASSWORD '…'``), set APP_DATABASE_URL, then
run migrations — or run this now and re-run the grants later via the
``rls_002`` rollout migration once the role exists.

Policy DDL comes from ``backend.app.rls`` so the statements shipped are the
statements the isolation tests prove. If you change a predicate in rls.py,
add a new re-apply migration.

Revision ID: rls_001_interactions
Revises: sr_001_interaction_step_runs
"""

import logging
import os

import sqlalchemy as sa

from alembic import op

revision = "rls_001_interactions"
# Re-parented onto sr_001 when this branch merged main: #164's step-run
# ledger landed as a sibling of fx_001 and two heads block every deploy.
down_revision = "sr_001_interaction_step_runs"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

SPIKE_TABLES = ["interactions"]


def _app_role(conn):
    role = os.environ.get("APP_DB_ROLE", "linda_app")
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar()
    return role, bool(exists)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return  # RLS is Postgres-only; nothing to do on other dialects

    from backend.app import rls

    for stmt in rls.bootstrap_statements():
        conn.execute(sa.text(stmt))
    for stmt in rls.rls_statements(tables=SPIKE_TABLES):
        conn.execute(sa.text(stmt))

    role, exists = _app_role(conn)
    if exists:
        for stmt in rls.grant_statements(role):
            conn.execute(sa.text(stmt))
    else:
        logger.warning(
            "RLS enabled on %s but app role %r does not exist yet — the app "
            "is still connecting as the owner and BYPASSING these policies. "
            "Create the role, set APP_DATABASE_URL, and re-run grants.",
            ", ".join(SPIKE_TABLES),
            role,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    for table in SPIKE_TABLES:
        for policy in (
            "tenant_isolation_select",
            "tenant_isolation_insert",
            "tenant_isolation_update",
            "tenant_isolation_delete",
        ):
            conn.execute(
                sa.text(
                    "DROP POLICY IF EXISTS {p} ON {t}".format(p=policy, t=table)
                )
            )
        conn.execute(
            sa.text("ALTER TABLE {t} DISABLE ROW LEVEL SECURITY".format(t=table))
        )
    conn.execute(sa.text("DROP FUNCTION IF EXISTS app_tenant_of_interaction(uuid)"))
