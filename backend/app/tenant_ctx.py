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

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional, Union
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
def tenant_context(tenant_id: Union[str, uuid.UUID]) -> Iterator[None]:
    """Scope all DB work inside the block to one tenant.

    The Celery entry point: beat jobs that iterate tenants open one of
    these per iteration (and must COMMIT before leaving the block so the
    next iteration's transaction re-arms with the next tenant).
    """
    token = set_current_tenant(tenant_id)
    try:
        yield
    finally:
        reset_current_tenant(token)


_RESOLVE_TENANT_SQL = text("SELECT app_tenant_of_interaction(:interaction_id)")
_FALLBACK_RESOLVE_SQL = text(
    "SELECT tenant_id FROM interactions WHERE id = :interaction_id"
)


def resolve_tenant_for_interaction(session, interaction_id) -> Optional[str]:
    """Look up an interaction's tenant so a Celery task can enter
    ``tenant_context`` — via the SECURITY DEFINER bootstrap function on
    Postgres (RLS would otherwise hide the row), or a direct read on
    other dialects (SQLite tests, dev without the migration).

    Ends the transaction it used (rollback — it only read), so the
    caller's next query starts a FRESH transaction that the listener can
    arm with the resolved tenant. Call it before any other work on the
    session. Returns None when the interaction doesn't exist.
    """
    try:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            row = session.execute(
                _RESOLVE_TENANT_SQL, {"interaction_id": str(interaction_id)}
            ).scalar()
        else:
            row = session.execute(
                _FALLBACK_RESOLVE_SQL, {"interaction_id": str(interaction_id)}
            ).scalar()
    finally:
        session.rollback()
    return str(row) if row is not None else None


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
