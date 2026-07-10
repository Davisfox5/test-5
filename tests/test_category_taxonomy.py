"""Tests for the category taxonomy service.

Normalization is pure-logic; the occurrence-recording tests run against a
sync in-memory SQLite (the service takes a sync Session — Celery's path).
The RLS half of the global-seed regression (the write policy actually
rejecting the UPDATE) lives in tests/test_rls_isolation.py, which needs a
real Postgres; here we prove the code never *attempts* to mutate a
tenant_id IS NULL row and that a failed flush can't poison the caller's
transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.app.services.category_taxonomy import (
    _normalize,
    record_occurrence,
)


def test_normalize_lowercases():
    assert _normalize("Follow Up") == "follow_up"


def test_normalize_strips_whitespace():
    assert _normalize("  follow_up  ") == "follow_up"


def test_normalize_collapses_multiple_spaces():
    assert _normalize("follow   up") == "follow_up"


def test_normalize_replaces_hyphens_with_underscores():
    assert _normalize("follow-up") == "follow_up"


def test_normalize_handles_mixed_separators():
    assert _normalize("Compliance Remediation") == "compliance_remediation"
    assert _normalize("compliance-remediation") == "compliance_remediation"
    assert _normalize("compliance_remediation") == "compliance_remediation"
    # All three variants normalize to the same canonical form.


def test_normalize_preserves_already_canonical():
    assert _normalize("commitment_made") == "commitment_made"


def test_normalize_handles_single_word():
    assert _normalize("escalation") == "escalation"
    assert _normalize("ESCALATION") == "escalation"


# ── record_occurrence vs global (tenant_id IS NULL) seed rows ────────────
#
# Regression for the RLS failure: resolving a category to a global seed
# row and bumping its occurrence_count flushes an UPDATE on a
# tenant_id IS NULL row, which the RLS write policy rejects from a tenant
# session — poisoning the whole analysis transaction. The fix records
# occurrences on a tenant-owned copy and never mutates the global row.


@pytest.fixture()
def taxonomy_db():
    """Sync in-memory SQLite with the full schema and one seeded tenant +
    the ``follow_up`` global seed row from migration aa1b2c3d4e5f."""
    import tests.db_fixtures  # noqa: F401 — registers sqlite JSONB/UUID compilers
    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers every mapped class
    from backend.app.models import CategoryTaxonomy, Tenant

    engine = create_engine("sqlite://")

    # pysqlite's implicit transaction handling breaks SAVEPOINT; take over
    # BEGIN so session.begin_nested() behaves like it does on Postgres.
    @event.listens_for(engine, "connect")
    def _disable_implicit_begin(dbapi_conn, _record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        tenant = Tenant(name="Taxonomy Tenant", slug="taxonomy-rls-test")
        session.add(tenant)
        session.flush()
        session.add(
            CategoryTaxonomy(
                tenant_id=None,
                canonical_name="follow_up",
                aliases=["followup", "follow up"],
                is_canonical=True,
                occurrence_count=0,
            )
        )
        session.commit()
        tenant_id = tenant.id

    with factory() as session:
        yield session, tenant_id
    engine.dispose()


def _taxonomy_rows(session, canonical_name):
    from backend.app.models import CategoryTaxonomy

    rows = session.execute(
        select(CategoryTaxonomy).where(
            CategoryTaxonomy.canonical_name == canonical_name
        )
    ).scalars().all()
    global_rows = [r for r in rows if r.tenant_id is None]
    tenant_rows = [r for r in rows if r.tenant_id is not None]
    return global_rows, tenant_rows


def test_global_seed_occurrence_creates_tenant_copy(taxonomy_db):
    session, tenant_id = taxonomy_db

    assert record_occurrence(session, tenant_id, "follow_up") == "follow_up"
    session.commit()

    global_rows, tenant_rows = _taxonomy_rows(session, "follow_up")
    assert len(global_rows) == 1 and len(tenant_rows) == 1
    # The global default is untouched — no UPDATE for RLS to reject.
    assert global_rows[0].occurrence_count == 0
    assert tenant_rows[0].tenant_id == tenant_id
    assert tenant_rows[0].occurrence_count == 1
    assert tenant_rows[0].is_canonical is True


def test_repeat_occurrences_bump_tenant_copy_not_global(taxonomy_db):
    session, tenant_id = taxonomy_db

    for _ in range(3):
        assert record_occurrence(session, tenant_id, "follow_up") == "follow_up"
    session.commit()

    global_rows, tenant_rows = _taxonomy_rows(session, "follow_up")
    assert global_rows[0].occurrence_count == 0
    assert len(tenant_rows) == 1  # bumped, not duplicated
    assert tenant_rows[0].occurrence_count == 3


def test_alias_of_global_seed_bumps_tenant_copy(taxonomy_db):
    session, tenant_id = taxonomy_db

    assert record_occurrence(session, tenant_id, "Follow Up") == "follow_up"
    assert record_occurrence(session, tenant_id, "followup") == "follow_up"
    session.commit()

    global_rows, tenant_rows = _taxonomy_rows(session, "follow_up")
    assert global_rows[0].occurrence_count == 0
    assert len(tenant_rows) == 1
    assert tenant_rows[0].occurrence_count == 2


def test_unknown_category_still_creates_tenant_candidate(taxonomy_db):
    session, tenant_id = taxonomy_db

    assert (
        record_occurrence(session, tenant_id, "hipaa review required")
        == "hipaa_review_required"
    )
    session.commit()

    global_rows, tenant_rows = _taxonomy_rows(session, "hipaa_review_required")
    assert global_rows == []
    assert tenant_rows[0].occurrence_count == 1
    assert tenant_rows[0].is_canonical is False


def test_failed_taxonomy_flush_does_not_poison_outer_transaction(
    taxonomy_db, monkeypatch
):
    """Defense in depth: even if the taxonomy write is rejected at flush
    time (as the RLS policy does in production), the savepoint absorbs it
    and the caller's pending work still commits."""
    from backend.app.models import CategoryTaxonomy, Tenant
    from backend.app.services import category_taxonomy as svc

    session, tenant_id = taxonomy_db

    def _broken_record(session, tenant_id, needle):
        # NOT NULL violation surfaces at the flush inside the savepoint,
        # standing in for Postgres's InsufficientPrivilege.
        session.add(
            CategoryTaxonomy(tenant_id=tenant_id, canonical_name=None)
        )
        return needle

    monkeypatch.setattr(svc, "_record_occurrence", _broken_record)

    # Outer-transaction work already pending before the taxonomy call.
    tenant = session.get(Tenant, tenant_id)
    tenant.name = "Renamed Mid-Analysis"

    assert record_occurrence(session, tenant_id, "follow_up") is None

    session.commit()  # must not raise PendingRollbackError
    session.expire_all()
    assert session.get(Tenant, tenant_id).name == "Renamed Mid-Analysis"
    global_rows, tenant_rows = _taxonomy_rows(session, "follow_up")
    assert global_rows[0].occurrence_count == 0 and tenant_rows == []
