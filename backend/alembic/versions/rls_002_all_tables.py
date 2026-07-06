"""Tenant RLS rollout: policies on every tenant-scoped table.

Extends the rls_001 spike (interactions only) to the full schema:

- installs/refreshes the SECURITY DEFINER tenant resolvers
  (backend.app.rls.TENANT_RESOLVER_FUNCTIONS);
- enables RLS + the four tenant_isolation_* policies on every table that
  carries a tenant_id column (backend.app.rls.tenant_scoped_tables()),
  with the bootstrap-read relaxation on api_keys / users / integrations /
  email_sync_cursors and NULL-row visibility on the intentionally-hybrid
  nullable tables;
- re-applies grants to the app role when it exists.

DDL comes from backend.app.rls so the statements shipped are exactly the
statements tests/test_rls_isolation.py proves. The table list is
intersected with what actually exists in the database, so replaying this
migration mid-chain (fresh environment where later migrations haven't
created their tables yet) never fails — but it also means A TABLE ADDED
AFTER THIS MIGRATION GETS NO POLICIES until its own policy migration
ships. tests/test_rls_scoping_guard.py enforces the new-table checklist.

Revision ID: rls_002_all_tables
Revises: rls_001_interactions
"""

import logging
import os

import sqlalchemy as sa

from alembic import op

revision = "rls_002_all_tables"
down_revision = "rls_001_interactions"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _existing_tables(conn):
    rows = conn.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    from backend.app import rls

    for stmt in rls.bootstrap_statements():
        conn.execute(sa.text(stmt))

    present = _existing_tables(conn)
    targets = [t for t in rls.tenant_scoped_tables() if t in present]
    missing = sorted(set(rls.tenant_scoped_tables()) - present)
    if missing:
        logger.warning(
            "RLS rollout: skipping tables not present in this database "
            "(created by later migrations; they get policies when their "
            "own migration ships): %s",
            ", ".join(missing),
        )
    for stmt in rls.rls_statements(tables=targets):
        conn.execute(sa.text(stmt))

    role = os.environ.get("APP_DB_ROLE", "linda_app")
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar()
    if exists:
        for stmt in rls.grant_statements(role):
            conn.execute(sa.text(stmt))
    else:
        logger.warning(
            "RLS policies applied but app role %r does not exist — the app "
            "still connects as the owner and BYPASSES them. Create the role, "
            "set APP_DATABASE_URL, and re-run grants.",
            role,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    from backend.app import rls

    present = _existing_tables(conn)
    for table in rls.tenant_scoped_tables():
        if table not in present or table == "interactions":
            continue  # interactions stays under rls_001
        for policy in (
            "tenant_isolation_select",
            "tenant_isolation_insert",
            "tenant_isolation_update",
            "tenant_isolation_delete",
        ):
            conn.execute(
                sa.text("DROP POLICY IF EXISTS {p} ON {t}".format(p=policy, t=table))
            )
        conn.execute(
            sa.text("ALTER TABLE {t} DISABLE ROW LEVEL SECURITY".format(t=table))
        )
