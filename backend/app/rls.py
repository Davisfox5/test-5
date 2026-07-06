"""Tenant row-level security: table classification + DDL emitters.

Single source of truth for WHICH tables are tenant-scoped and WHAT the
policies say. The Alembic migrations and the isolation tests both execute
the emitters here, so the policies proven by ``tests/test_rls_isolation.py``
are byte-for-byte the policies shipped. If you change a predicate here,
add a re-apply migration — existing databases keep whatever policies their
last migration installed.

Design (docs/complexity/04-tenant-isolation-migration.md §7-§8):

- The app connects as a NON-OWNER role (``APP_DATABASE_URL``); the owner
  connection (``DATABASE_URL``) is the bypass path for Alembic and admin
  work, enforced by Postgres itself (owners bypass non-FORCED RLS).
- The tenant is carried by the ``app.current_tenant`` GUC, re-armed on
  every transaction by ``backend.app.tenant_ctx``. GUC unset → the
  predicate is NULL → **zero rows, fail closed**.
- ``AUTH_BOOTSTRAP_TABLES`` (api_keys, users) additionally allow SELECT
  while the GUC is unset: ``get_current_principal`` has to look up the
  credential before any tenant is known. Writes to those tables still
  require a matching tenant.
- Tables whose ``tenant_id`` is nullable treat NULL rows as global
  (readable by every tenant, writable only through the owner path).
"""

from typing import Iterable, List, Optional

GUC_NAME = "app.current_tenant"

# Tables with NO tenant_id column, global by design — the allow-list.
# tests/test_rls_scoping_guard.py fails if a new table appears that is
# neither tenant-scoped nor listed here.
GLOBAL_TABLES = frozenset(
    {
        "tenants",  # the root of tenancy itself
        "prompt_variants",  # versioned prompts, cross-tenant by design
        "experiments",  # global experiment catalog
        "cross_tenant_analytics",  # aggregate metrics, no tenant_id by design
        "llm_ceiling_recommendation",  # system-level LLM telemetry aggregate
        "demo_email_captures",  # marketing lead capture, pre-tenant
    }
)

# Read *before* a tenant is resolved; SELECT is allowed with the GUC
# unset, writes are not. Two families:
# - credential lookup: get_current_principal must find the api key / user
#   before it knows the tenant;
# - webhook correlation: provider callbacks (Gmail/Graph push) locate the
#   integration / sync cursor from provider-side identifiers (account
#   email, subscription id) that don't carry our tenant id. Handlers bind
#   the tenant immediately after this first lookup.
# Every other tenant-scoped table stays strict: no GUC → zero rows.
AUTH_BOOTSTRAP_TABLES = frozenset(
    {"api_keys", "users", "integrations", "email_sync_cursors"}
)

# The GUC as a nullable uuid: NULL when unset or empty.
_TENANT_EXPR = "NULLIF(current_setting('{guc}', true), '')::uuid".format(guc=GUC_NAME)


def tenant_scoped_tables() -> List[str]:
    """Every ORM table carrying a tenant_id column, minus the allow-list."""
    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers every mapped class

    scoped = []
    for table in Base.metadata.sorted_tables:
        if table.name in GLOBAL_TABLES:
            continue
        if "tenant_id" in table.columns:
            scoped.append(table.name)
    return scoped


def _is_nullable(table_name: str) -> bool:
    from backend.app.db import Base

    table = Base.metadata.tables[table_name]
    return bool(table.columns["tenant_id"].nullable)


def _read_predicate(table_name: str) -> str:
    pred = "tenant_id = {expr}".format(expr=_TENANT_EXPR)
    if _is_nullable(table_name):
        pred = "(tenant_id IS NULL OR {pred})".format(pred=pred)
    if table_name in AUTH_BOOTSTRAP_TABLES:
        pred = "({pred} OR {expr} IS NULL)".format(pred=pred, expr=_TENANT_EXPR)
    return pred


def _write_predicate(table_name: str) -> str:
    # Writes always require a matching tenant — no bootstrap window, and no
    # NULL-tenant writes (global rows are managed through the owner path).
    return "tenant_id = {expr}".format(expr=_TENANT_EXPR)


def rls_statements(tables: Optional[Iterable[str]] = None) -> List[str]:
    """Idempotent DDL enabling RLS + policies on the given tables.

    ``tables`` defaults to every tenant-scoped table. Emits DROP POLICY IF
    EXISTS before each CREATE POLICY so re-running is safe.
    """
    stmts = []  # type: List[str]
    names = list(tables) if tables is not None else tenant_scoped_tables()
    for name in names:
        stmts.append(
            "ALTER TABLE {t} ENABLE ROW LEVEL SECURITY".format(t=name)
        )
        stmts.append(
            "DROP POLICY IF EXISTS tenant_isolation_select ON {t}".format(t=name)
        )
        stmts.append(
            "CREATE POLICY tenant_isolation_select ON {t} FOR SELECT "
            "USING ({pred})".format(t=name, pred=_read_predicate(name))
        )
        stmts.append(
            "DROP POLICY IF EXISTS tenant_isolation_insert ON {t}".format(t=name)
        )
        stmts.append(
            "CREATE POLICY tenant_isolation_insert ON {t} FOR INSERT "
            "WITH CHECK ({pred})".format(t=name, pred=_write_predicate(name))
        )
        stmts.append(
            "DROP POLICY IF EXISTS tenant_isolation_update ON {t}".format(t=name)
        )
        stmts.append(
            "CREATE POLICY tenant_isolation_update ON {t} FOR UPDATE "
            "USING ({read}) WITH CHECK ({write})".format(
                t=name,
                read=_read_predicate(name),
                write=_write_predicate(name),
            )
        )
        stmts.append(
            "DROP POLICY IF EXISTS tenant_isolation_delete ON {t}".format(t=name)
        )
        stmts.append(
            "CREATE POLICY tenant_isolation_delete ON {t} FOR DELETE "
            "USING ({pred})".format(t=name, pred=_read_predicate(name))
        )
    return stmts


# Celery tasks receive a row id (interaction, integration, backfill job…)
# but no tenant — and RLS won't let them read that row to find out whose
# it is. These SECURITY DEFINER functions are the narrow bootstrap: each
# maps one id → tenant id (one owner-privileged indexed lookup, nothing
# else) so the task can enter tenant_context() and do everything else
# under RLS. Keyed by table; the function name is app_tenant_of_<singular>.
TENANT_RESOLVER_FUNCTIONS = {
    "interactions": "app_tenant_of_interaction",
    "integrations": "app_tenant_of_integration",
    "email_backfill_jobs": "app_tenant_of_email_backfill_job",
    "support_cases": "app_tenant_of_support_case",
    "manager_recommendations": "app_tenant_of_manager_recommendation",
    "webhook_deliveries": "app_tenant_of_webhook_delivery",
    "live_sessions": "app_tenant_of_live_session",
}


def bootstrap_statements() -> List[str]:
    stmts = []  # type: List[str]
    for table, func_name in sorted(TENANT_RESOLVER_FUNCTIONS.items()):
        stmts.append(
            "CREATE OR REPLACE FUNCTION {f}(iid uuid) "
            "RETURNS uuid "
            "LANGUAGE sql STABLE SECURITY DEFINER "
            "SET search_path = public "
            "AS $$ SELECT tenant_id FROM {t} WHERE id = iid $$".format(
                f=func_name, t=table
            )
        )
    return stmts


def runtime_bypasses_rls(sync_connection) -> Optional[str]:
    """Why the current connection would skip RLS, or None if it enforces it.

    Called from the API lifespan (via ``conn.run_sync``) so startup logs
    say whether the backstop is actually live. Checks the three bypass
    routes: superuser, BYPASSRLS, and table ownership (owners skip
    non-FORCEd policies).
    """
    from sqlalchemy import text as _text

    row = sync_connection.execute(
        _text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
    ).first()
    if row is None:
        return None
    if row[0]:
        return "connected as a superuser ({0})".format(_current_user(sync_connection))
    if row[1]:
        return "role {0} has BYPASSRLS".format(_current_user(sync_connection))
    owns = sync_connection.execute(
        _text(
            "SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE c.relname = 'interactions' AND r.rolname = current_user"
        )
    ).scalar()
    if owns:
        return "role {0} owns the tables".format(_current_user(sync_connection))
    return None


def _current_user(sync_connection) -> str:
    from sqlalchemy import text as _text

    return str(sync_connection.execute(_text("SELECT current_user")).scalar())


def grant_statements(role: str) -> List[str]:
    """Grants letting a non-owner app role use the schema (RLS still applies)."""
    return [
        "GRANT USAGE ON SCHEMA public TO {r}".format(r=role),
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}".format(r=role),
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}".format(r=role),
        # Future tables created by the owner (migrations) inherit the grants.
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}".format(r=role),
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO {r}".format(r=role),
    ]
