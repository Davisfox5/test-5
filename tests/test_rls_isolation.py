"""Cross-tenant isolation tests for Postgres row-level security.

These tests need a REAL Postgres (RLS doesn't exist in SQLite, and the
owner-vs-app-role split is the whole point). They connect to
``TEST_POSTGRES_URL`` (an OWNER-privileged DSN), build the schema, create
the non-owner ``linda_app_test`` role, apply the same RLS DDL the Alembic
migration uses (via ``backend.app.rls``), and then prove:

- through the ASYNC engine (asyncpg, what the API uses): a session bound
  to tenant A cannot read tenant B's interactions — zero rows, not an error;
- through the SYNC engine (psycopg2, what Celery uses): same guarantee;
- with NO tenant context at all: zero rows (fail closed);
- the WITH CHECK clause rejects writes that smuggle another tenant's id;
- the owner connection still sees everything (the migrations/admin path).

Skipped automatically when no Postgres is reachable (CI without the
service container, or a laptop without the docker container running —
see docs/complexity/04-tenant-isolation-migration.md §7).
"""

import os
import socket
import uuid
from urllib.parse import urlparse

import pytest

TEST_POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql://linda_owner:test@localhost:55432/linda_test",
)

APP_ROLE = "linda_app_test"
APP_ROLE_PASSWORD = "app-test-pw"


def _postgres_reachable() -> bool:
    parsed = urlparse(TEST_POSTGRES_URL)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=2
        ):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="no Postgres reachable at TEST_POSTGRES_URL — RLS tests need a real Postgres",
)


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _app_role_url(url: str, driver_prefix: str) -> str:
    """Rewrite the owner DSN to connect as the non-owner app role."""
    parsed = urlparse(url)
    netloc = "{0}:{1}@{2}:{3}".format(
        APP_ROLE, APP_ROLE_PASSWORD, parsed.hostname, parsed.port or 5432
    )
    return "{0}://{1}{2}".format(driver_prefix, netloc, parsed.path)


@pytest.fixture(scope="module")
def rls_database():
    """Build schema + roles + RLS policies once for the module; seed two tenants.

    Returns (tenant_a_id, tenant_b_id, interaction_a_id, interaction_b_id).
    Synchronous fixture (psycopg2 + sync SQLAlchemy) so it works regardless
    of pytest-asyncio loop scoping.
    """
    from sqlalchemy import create_engine, text

    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers every mapped class
    from backend.app import rls

    owner = create_engine(TEST_POSTGRES_URL, isolation_level="AUTOCOMMIT")

    with owner.connect() as conn:
        # Fresh schema every run — this is a disposable test database.
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    # pgvector types may not exist in the vanilla postgres image; the models
    # that need it use plain ARRAY(Float) — create_all needs no extensions.
    Base.metadata.create_all(owner)

    with owner.connect() as conn:
        conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        CREATE ROLE {role} LOGIN PASSWORD '{pw}';
                    END IF;
                END $$
                """.format(role=APP_ROLE, pw=APP_ROLE_PASSWORD)
            )
        )
        for stmt in rls.grant_statements(APP_ROLE):
            conn.execute(text(stmt))
        for stmt in rls.bootstrap_statements():
            conn.execute(text(stmt))
        # Full rollout — the same statement set migration rls_002 applies.
        for stmt in rls.rls_statements():
            conn.execute(text(stmt))

    # Seed two tenants, each with one interaction and one user, plus a
    # NULL-tenant (global) reference set — as the owner (bypasses RLS).
    # ORM so Python-side column defaults apply.
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import (
        EvaluationReferenceSet,
        Interaction,
        Tenant,
        User,
    )

    factory = sessionmaker(bind=owner, expire_on_commit=False)
    with factory() as session:
        tenant_row_a = Tenant(name="Tenant A", slug="tenant-a-rls")
        tenant_row_b = Tenant(name="Tenant B", slug="tenant-b-rls")
        session.add_all([tenant_row_a, tenant_row_b])
        session.flush()
        inter_row_a = Interaction(tenant_id=tenant_row_a.id, channel="voice")
        inter_row_b = Interaction(tenant_id=tenant_row_b.id, channel="voice")
        session.add_all(
            [
                inter_row_a,
                inter_row_b,
                User(tenant_id=tenant_row_a.id, email="a@rls.test"),
                User(tenant_id=tenant_row_b.id, email="b@rls.test"),
                EvaluationReferenceSet(
                    tenant_id=None, name="global-rls-ref", surface="analysis"
                ),
            ]
        )
        session.commit()
        tenant_a, tenant_b = tenant_row_a.id, tenant_row_b.id
        inter_a, inter_b = inter_row_a.id, inter_row_b.id

    owner.dispose()
    return tenant_a, tenant_b, inter_a, inter_b


# ── async engine (the API path) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_engine_cross_tenant_read_returns_zero_rows(rls_database):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.models import Interaction
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_async_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql+asyncpg"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            with tenant_context(str(tenant_a)):
                # Own row: visible.
                own = (
                    await session.execute(
                        select(Interaction).where(Interaction.id == inter_a)
                    )
                ).scalars().all()
                assert len(own) == 1

                # Other tenant's row, queried BY PRIMARY KEY with no tenant
                # filter — the exact "forgotten filter" bug. RLS must return
                # zero rows.
                leaked = (
                    await session.execute(
                        select(Interaction).where(Interaction.id == inter_b)
                    )
                ).scalars().all()
                assert leaked == []

                # Unfiltered scan: only tenant A's rows.
                all_visible = (
                    await session.execute(select(Interaction.tenant_id).distinct())
                ).scalars().all()
                assert {str(t) for t in all_visible} == {str(tenant_a)}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_engine_no_tenant_context_fails_closed(rls_database):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.models import Interaction

    engine = create_async_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql+asyncpg"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            rows = (await session.execute(select(Interaction))).scalars().all()
            assert rows == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_engine_guc_survives_mid_request_commit(rls_database):
    """Handlers commit mid-request; the listener must re-arm the GUC on the
    next transaction, not just the first one."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.models import Interaction
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_async_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql+asyncpg"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            with tenant_context(str(tenant_a)):
                first = (
                    await session.execute(select(Interaction))
                ).scalars().all()
                assert len(first) == 1
                await session.commit()  # ends the transaction — SET LOCAL dies here

                second = (
                    await session.execute(select(Interaction))
                ).scalars().all()
                assert len(second) == 1
                assert str(second[0].tenant_id) == str(tenant_a)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_engine_with_check_blocks_cross_tenant_insert(rls_database):
    from sqlalchemy.exc import DBAPIError, ProgrammingError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.models import Interaction
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_async_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql+asyncpg"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            with tenant_context(str(tenant_a)):
                session.add(Interaction(tenant_id=tenant_b, channel="voice"))
                with pytest.raises((DBAPIError, ProgrammingError)):
                    await session.flush()
                await session.rollback()
    finally:
        await engine.dispose()


# ── sync engine (the Celery path) ─────────────────────────────────────────


def test_sync_engine_cross_tenant_read_returns_zero_rows(rls_database):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Interaction
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            with tenant_context(str(tenant_b)):
                own = session.execute(
                    select(Interaction).where(Interaction.id == inter_b)
                ).scalars().all()
                assert len(own) == 1

                leaked = session.execute(
                    select(Interaction).where(Interaction.id == inter_a)
                ).scalars().all()
                assert leaked == []
    finally:
        engine.dispose()


def test_sync_engine_tenant_switch_within_one_session(rls_database):
    """Beat jobs iterate tenants inside ONE session — each iteration must see
    exactly its own tenant's rows."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Interaction
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            for tenant_id, expected_interaction in (
                (tenant_a, inter_a),
                (tenant_b, inter_b),
            ):
                with tenant_context(str(tenant_id)):
                    rows = session.execute(select(Interaction)).scalars().all()
                    assert [r.id for r in rows] == [expected_interaction]
                session.commit()  # close the txn so the next iteration re-arms
    finally:
        engine.dispose()


def test_sync_engine_no_context_fails_closed(rls_database):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Interaction

    engine = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            assert session.execute(select(Interaction)).scalars().all() == []
    finally:
        engine.dispose()


def test_celery_bootstrap_resolver_under_rls(rls_database):
    """A worker gets an interaction_id it cannot read (RLS, no context yet).
    The SECURITY DEFINER resolver must hand back the tenant so the task can
    enter tenant_context and then see exactly that tenant's rows."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Interaction
    from backend.app.tenant_ctx import (
        resolve_tenant_for_interaction,
        tenant_context,
    )

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            resolved = resolve_tenant_for_interaction(session, inter_a)
            assert resolved == str(tenant_a)

            with tenant_context(resolved):
                rows = session.execute(select(Interaction)).scalars().all()
                assert [r.id for r in rows] == [inter_a]

            assert resolve_tenant_for_interaction(session, uuid.uuid4()) is None
    finally:
        engine.dispose()


# ── the bypass path (owner == migrations/admin) ───────────────────────────


def test_owner_connection_bypasses_rls(rls_database):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Interaction

    engine = create_engine(TEST_POSTGRES_URL)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            rows = session.execute(select(Interaction)).scalars().all()
            assert len(rows) >= 2  # sees both tenants' rows
    finally:
        engine.dispose()


def test_bootstrap_tables_readable_pre_auth_but_write_locked(rls_database):
    """users/api_keys/integrations/email_sync_cursors: SELECT works with NO
    tenant bound (auth + webhook correlation need it), scoped once bound,
    and writes always require a matching tenant."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.exc import DBAPIError, ProgrammingError
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import User
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            # Pre-auth: both tenants' users are visible (credential lookup).
            emails = {
                u.email for u in session.execute(select(User)).scalars().all()
            }
            assert {"a@rls.test", "b@rls.test"} <= emails
            session.rollback()

            # Bound: scoped to the tenant.
            with tenant_context(str(tenant_a)):
                emails = {
                    u.email
                    for u in session.execute(select(User)).scalars().all()
                }
                assert "a@rls.test" in emails and "b@rls.test" not in emails
            session.rollback()

            # Unbound write: rejected — the pre-auth window is read-only.
            session.add(User(tenant_id=tenant_a, email="smuggled@rls.test"))
            with pytest.raises((DBAPIError, ProgrammingError)):
                session.flush()
            session.rollback()
    finally:
        engine.dispose()


def test_nullable_tenant_rows_are_global_reads_only(rls_database):
    """NULL-tenant rows on intentionally-hybrid tables are readable by
    every tenant; writing a NULL-tenant row through the app role is not."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.exc import DBAPIError, ProgrammingError
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import EvaluationReferenceSet
    from backend.app.tenant_ctx import tenant_context

    tenant_a, tenant_b, inter_a, inter_b = rls_database
    engine = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            for tid in (tenant_a, tenant_b):
                with tenant_context(str(tid)):
                    names = {
                        r.name
                        for r in session.execute(
                            select(EvaluationReferenceSet)
                        ).scalars().all()
                    }
                    assert "global-rls-ref" in names
                session.rollback()

            with tenant_context(str(tenant_a)):
                session.add(
                    EvaluationReferenceSet(
                        tenant_id=None, name="smuggled-global", surface="analysis"
                    )
                )
                with pytest.raises((DBAPIError, ProgrammingError)):
                    session.flush()
                session.rollback()
    finally:
        engine.dispose()


def test_startup_posture_check_tells_owner_from_app_role(rls_database):
    """The lifespan warning must fire for the owner DSN and stay quiet for
    the enforcing app role."""
    from sqlalchemy import create_engine

    from backend.app.rls import runtime_bypasses_rls

    owner = create_engine(TEST_POSTGRES_URL)
    app_role = create_engine(_app_role_url(TEST_POSTGRES_URL, "postgresql"))
    try:
        with owner.connect() as conn:
            assert runtime_bypasses_rls(conn) is not None
        with app_role.connect() as conn:
            assert runtime_bypasses_rls(conn) is None
    finally:
        owner.dispose()
        app_role.dispose()
