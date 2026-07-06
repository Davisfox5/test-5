"""Request/task-scoped tenant context that rides every Postgres transaction.

The RLS policies (see ``backend.app.rls``) key on the ``app.current_tenant``
GUC. ``SET LOCAL`` semantics mean the GUC dies at every COMMIT/ROLLBACK —
and API handlers commit mid-request — so a one-shot ``SET`` is not enough.
Instead:

- trusted code (``auth.get_current_principal`` for the API, a
  ``tenant_context(...)`` block for Celery) stores the tenant id in a
  ContextVar;
- a global ``after_begin`` session listener re-issues
  ``set_config('app.current_tenant', <id>, is_local=true)`` at the start of
  EVERY transaction, on both the async (asyncpg) and sync (psycopg2)
  engines.

The ContextVar here is set only from authenticated state. It is distinct
from ``logging_setup.tenant_id_var``, which the request middleware seeds
from the *unverified* ``X-Tenant-Id`` header for log correlation — that
value must never reach the GUC.

If no tenant is set, the listener does nothing and the policies see an
unset GUC: zero rows, fail closed.
"""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from typing import AsyncIterator, Iterator, Optional, Union
import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from backend.app.rls import GUC_NAME

_current_tenant_id: ContextVar[Optional[str]] = ContextVar(
    "rls_current_tenant_id", default=None
)

# Shared by the listener below and by auth.py, which must arm the GUC on a
# transaction that is ALREADY open (the credential lookup began it before
# the tenant was known).
SET_TENANT_GUC_SQL = text(
    "SELECT set_config('{guc}', :tenant_id, true)".format(guc=GUC_NAME)
)


def get_current_tenant_id() -> Optional[str]:
    return _current_tenant_id.get()


def set_current_tenant(tenant_id: Union[str, uuid.UUID]) -> Token:
    """Bind the tenant for the current async task / thread context.

    Returns the ContextVar token so callers that must restore the previous
    value (nested admin flows, tests) can ``reset_current_tenant(token)``.
    """
    return _current_tenant_id.set(str(tenant_id))


def reset_current_tenant(token: Token) -> None:
    _current_tenant_id.reset(token)


@contextmanager
def tenant_context(
    tenant_id: Union[str, uuid.UUID], session=None
) -> Iterator[None]:
    """Scope all DB work inside the block to one tenant.

    The Celery entry point: all-tenant beat jobs open one of these per
    loop iteration. Pass the (sync) ``session`` when a transaction may
    already be open — e.g. the tenants-list query that feeds the loop —
    so the GUC is re-pointed on the CURRENT transaction too, not just on
    the next one the after_begin listener sees.
    """
    token = set_current_tenant(tenant_id)
    try:
        if (
            session is not None
            and session.bind is not None
            and session.bind.dialect.name == "postgresql"
        ):
            session.execute(SET_TENANT_GUC_SQL, {"tenant_id": str(tenant_id)})
        yield
    finally:
        reset_current_tenant(token)


@asynccontextmanager
async def tenant_context_async(
    tenant_id: Union[str, uuid.UUID], db=None
) -> AsyncIterator[None]:
    """Async twin of :func:`tenant_context` for AsyncSession loops."""
    token = set_current_tenant(tenant_id)
    try:
        if db is not None:
            await bind_tenant_async(db, tenant_id)
        yield
    finally:
        reset_current_tenant(token)


async def bind_tenant_async(db, tenant_id: Union[str, uuid.UUID]) -> None:
    """Bind the tenant on an AsyncSession whose transaction may already be
    open (auth deps, webhook handlers): sets the ContextVar for every
    LATER transaction and set_config's the CURRENT one. Idempotent.
    """
    set_current_tenant(tenant_id)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(SET_TENANT_GUC_SQL, {"tenant_id": str(tenant_id)})


def resolve_tenant_via(session, table: str, row_id) -> Optional[str]:
    """Map a row id in ``table`` to its tenant so a Celery task can enter
    ``tenant_context`` — via that table's SECURITY DEFINER bootstrap
    function on Postgres (RLS would otherwise hide the row), or a direct
    read on other dialects (SQLite tests, dev without the migration).

    Ends the transaction it used (rollback — it only read), so the
    caller's next query starts a FRESH transaction that the listener can
    arm with the resolved tenant. Call it before any other work on the
    session. Returns None when the row doesn't exist.
    """
    from backend.app.rls import TENANT_RESOLVER_FUNCTIONS

    func_name = TENANT_RESOLVER_FUNCTIONS[table]  # KeyError = missing resolver, fail loud
    try:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            row = session.execute(
                text("SELECT {f}(:row_id)".format(f=func_name)),
                {"row_id": str(row_id)},
            ).scalar()
        else:
            row = session.execute(
                text(
                    "SELECT tenant_id FROM {t} WHERE id = :row_id".format(t=table)
                ),
                {"row_id": str(row_id)},
            ).scalar()
    finally:
        session.rollback()
    return str(row) if row is not None else None


async def resolve_tenant_via_async(db, table: str, row_id) -> Optional[str]:
    """Async twin of :func:`resolve_tenant_via` for webhook/WebSocket
    handlers on the API engine."""
    from backend.app.rls import TENANT_RESOLVER_FUNCTIONS

    func_name = TENANT_RESOLVER_FUNCTIONS[table]
    try:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            row = (
                await db.execute(
                    text("SELECT {f}(:row_id)".format(f=func_name)),
                    {"row_id": str(row_id)},
                )
            ).scalar()
        else:
            row = (
                await db.execute(
                    text(
                        "SELECT tenant_id FROM {t} WHERE id = :row_id".format(t=table)
                    ),
                    {"row_id": str(row_id)},
                )
            ).scalar()
    finally:
        await db.rollback()
    return str(row) if row is not None else None


def resolve_tenant_for_interaction(session, interaction_id) -> Optional[str]:
    return resolve_tenant_via(session, "interactions", interaction_id)


@event.listens_for(Session, "after_begin", propagate=True)
def _arm_tenant_guc(session, transaction, connection) -> None:
    if transaction.nested:
        return  # SAVEPOINT — the outer transaction's GUC still applies
    if connection.dialect.name != "postgresql":
        return  # SQLite test fixtures etc.
    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        return  # fail closed: policies see an unset GUC → zero rows
    connection.execute(SET_TENANT_GUC_SQL, {"tenant_id": tenant_id})
