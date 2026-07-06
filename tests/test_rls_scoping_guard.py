"""Scoping guard: every table must declare its tenancy, and RLS must cover it.

Pure ORM-metadata assertions — no database needed, so this runs in every
environment (including SQLite CI) and fails the build the moment someone
adds a table that is neither tenant-scoped nor consciously global.

The new-table checklist this enforces
(docs/complexity/04-tenant-isolation-migration.md §8):

1. Give the table a ``tenant_id`` column (FK to tenants.id) — it is then
   automatically covered by ``backend.app.rls`` and the next policy
   migration — OR add it to ``rls.GLOBAL_TABLES`` with a comment saying
   why it is global by design.
2. If the table must be readable BEFORE a tenant is authenticated
   (credential lookups, webhook correlation), add it to
   ``rls.AUTH_BOOTSTRAP_TABLES`` — reads are open pre-auth, writes still
   require the tenant GUC.
3. Ship the RLS policy migration for it (see rls_00x migrations).
"""

from backend.app import rls
from backend.app.db import Base
import backend.app.models  # noqa: F401 — registers every mapped class


def _all_tables():
    return list(Base.metadata.sorted_tables)


def test_every_table_declares_its_tenancy():
    """A table either carries tenant_id or is on the explicit global list."""
    unclassified = []
    for table in _all_tables():
        has_tenant = "tenant_id" in table.columns
        is_global = table.name in rls.GLOBAL_TABLES
        if not has_tenant and not is_global:
            unclassified.append(table.name)
    assert unclassified == [], (
        "Tables with no tenant_id that are not on the rls.GLOBAL_TABLES "
        "allow-list: {0}. Either add a tenant_id column (tenant-scoped) or "
        "add them to GLOBAL_TABLES with a why-comment (global by design). "
        "See the new-table checklist in tests/test_rls_scoping_guard.py."
        .format(unclassified)
    )


def test_global_allowlist_has_no_stale_entries():
    """Nothing on the allow-list secretly grew a tenant_id column, and
    nothing on it vanished from the schema (stale entries hide gaps)."""
    table_names = {t.name for t in _all_tables()}
    for name in rls.GLOBAL_TABLES:
        assert name in table_names, (
            "rls.GLOBAL_TABLES entry {0!r} is not a table anymore — remove it"
            .format(name)
        )
        assert "tenant_id" not in Base.metadata.tables[name].columns, (
            "{0!r} is on the global allow-list but HAS a tenant_id column — "
            "it must be tenant-scoped (remove it from GLOBAL_TABLES)"
            .format(name)
        )


def test_rls_statements_cover_every_tenant_scoped_table():
    covered = set()
    for stmt in rls.rls_statements():
        if stmt.startswith("ALTER TABLE ") and "ENABLE ROW LEVEL SECURITY" in stmt:
            covered.add(stmt.split()[2])
    expected = set(rls.tenant_scoped_tables())
    assert covered == expected


def test_bootstrap_tables_are_tenant_scoped_tables():
    """The pre-auth-readable set must be real tenant-scoped tables (a typo
    here would silently apply no policy relaxation at all)."""
    scoped = set(rls.tenant_scoped_tables())
    for name in rls.AUTH_BOOTSTRAP_TABLES:
        assert name in scoped, (
            "rls.AUTH_BOOTSTRAP_TABLES entry {0!r} is not a tenant-scoped "
            "table".format(name)
        )


def test_tenant_id_is_not_nullable_unless_intentionally_hybrid():
    """Nullable tenant_id means 'NULL row = visible to every tenant'. That
    must be a conscious choice — list membership here is the declaration."""
    intentionally_hybrid = {
        "category_taxonomy",
        "scorer_versions",
        "evaluation_reference_sets",
        "dropped_outcome_events",
        "llm_call_telemetry",
    }
    surprise_nullable = []
    for name in rls.tenant_scoped_tables():
        col = Base.metadata.tables[name].columns["tenant_id"]
        if col.nullable and name not in intentionally_hybrid:
            surprise_nullable.append(name)
    assert surprise_nullable == [], (
        "Tables with a NULLABLE tenant_id not declared as intentionally "
        "hybrid: {0}. NULL rows are readable by EVERY tenant under the RLS "
        "policy — make tenant_id NOT NULL or add the table to the "
        "intentionally_hybrid set here with a why-comment."
        .format(surprise_nullable)
    )
