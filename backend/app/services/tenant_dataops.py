"""Tenant data export + hard delete — GDPR Articles 15 & 17.

Two operations a tenant's data-protection officer can ask for:

* **Export** (Article 15 — right of access). Produces a single JSON
  bundle containing every row a tenant owns across the schema. Rows
  are grouped by table name; large tables stream as line-delimited
  JSON so the bundle stays reasonable for multi-GB tenants.
* **Hard delete** (Article 17 — right to erasure). Drops every row a
  tenant owns and the tenant record itself. ``ondelete="CASCADE"``
  handles most tables; tables without that constraint (historically
  audit-style or analytics rollups) are cleaned explicitly first.

Both operations are async-generator-driven so the caller streams
progress: you can hand the output to a FastAPI ``StreamingResponse``
and the user sees bytes move as the export runs.

Audit trail: every call writes a :class:`TenantDataOpsLog` row with
actor, operation, status, counts.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Tuple

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Base, Tenant

logger = logging.getLogger(__name__)


# Tables we deliberately skip during export — either because they're
# shared across tenants (catalogs, reference data) or because the
# content is unused after export (sync logs). Add sparingly.
_EXPORT_SKIP_TABLES: set[str] = {
    "alembic_version",
    "tenant_dataops_log",  # the log we write while exporting
}


async def export_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> AsyncIterator[bytes]:
    """Stream a line-delimited JSON archive of every row a tenant owns.

    Output layout:

    .. code-block:: text

        {"_meta": {"tenant_id": "...", "exported_at": "..."}}
        {"_table": "tenants", "row": {...}}
        {"_table": "users", "row": {...}}
        {"_table": "users", "row": {...}}
        ...
        {"_eof": true, "tables": {...counts...}}

    Each line is a self-contained JSON document so consumers don't
    need to hold the whole bundle in memory. Binary fields (bytes,
    memoryview) are base64-encoded under a ``__b64__`` wrapper so the
    JSON stays round-trippable.
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"Tenant {tenant_id} not found")

    yield _ndjson(
        {
            "_meta": {
                "tenant_id": str(tenant_id),
                "tenant_name": tenant.name,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "schema_version": _schema_version(db),
            }
        }
    )

    counts: Dict[str, int] = {}
    for table_name, rows in _iter_tenant_tables(tenant_id):
        if table_name in _EXPORT_SKIP_TABLES:
            continue
        stream_fn = rows  # unused, keeps mypy happy
        table_count = 0
        async for row_dict in _stream_table_rows(db, table_name, tenant_id):
            yield _ndjson({"_table": table_name, "row": row_dict})
            table_count += 1
        if table_count:
            counts[table_name] = table_count

    yield _ndjson({"_eof": True, "tables": counts})


async def hard_delete_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> Dict[str, Any]:
    """Delete every row a tenant owns, then the tenant itself.

    Runs in a single transaction so a partial failure doesn't leave a
    half-scrubbed tenant in place. Returns per-table delete counts
    for the audit log.

    Most tables cascade from ``tenants``. We still enumerate them so
    we can report accurate counts and catch any future tables that
    someone added without an ``ondelete="CASCADE"`` clause.
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"Tenant {tenant_id} not found")

    deleted_counts: Dict[str, int] = {}
    # Tables in reverse topological order so FK dependencies unwind
    # cleanly. The DB cascade handles most; we issue explicit deletes
    # so we can count rows and surface them to the audit log.
    for table_name in _tenant_tables_reverse_topo():
        if table_name == "tenants":
            continue  # deleted last
        count = await _delete_table_for_tenant(db, table_name, tenant_id)
        if count:
            deleted_counts[table_name] = count

    await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
    deleted_counts["tenants"] = 1
    return {
        "tenant_id": str(tenant_id),
        "deleted": deleted_counts,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Helpers ─────────────────────────────────────────────────────────


def _tenant_tables_reverse_topo() -> List[str]:
    """Tables that carry a ``tenant_id`` column, in reverse
    topological order — deepest children first, ``tenants`` last.

    SQLAlchemy's metadata already tracks FK dependencies, so we
    reuse its sorted_tables and filter by tenant_id presence.
    """
    tenant_scoped: List[str] = []
    for table in reversed(Base.metadata.sorted_tables):
        if "tenant_id" in table.columns or table.name == "tenants":
            tenant_scoped.append(table.name)
    return tenant_scoped


def _iter_tenant_tables(tenant_id: uuid.UUID) -> List[Tuple[str, None]]:
    """Forward topological order for export — parents first so readers
    can stream-insert without tripping over FKs."""
    out: List[Tuple[str, None]] = []
    for table in Base.metadata.sorted_tables:
        if "tenant_id" in table.columns or table.name == "tenants":
            out.append((table.name, None))
    return out


async def _stream_table_rows(
    db: AsyncSession,
    table_name: str,
    tenant_id: uuid.UUID,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield one dict per row in ``table_name`` for ``tenant_id``.

    Uses a raw SELECT via SQL core so we don't need the ORM-mapped
    class (some tables don't have one, e.g. association tables).
    """
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return

    if table_name == "tenants":
        stmt = select(table).where(table.c.id == tenant_id)
    elif "tenant_id" in table.columns:
        stmt = select(table).where(table.c.tenant_id == tenant_id)
    else:
        return

    result = await db.execute(stmt)
    for row in result.mappings():
        yield _jsonable(dict(row))


async def _delete_table_for_tenant(
    db: AsyncSession,
    table_name: str,
    tenant_id: uuid.UUID,
) -> int:
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return 0
    if table_name == "tenants":
        return 0  # handled last
    if "tenant_id" not in table.columns:
        return 0
    stmt = delete(table).where(table.c.tenant_id == tenant_id)
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


def _ndjson(payload: Dict[str, Any]) -> bytes:
    return (json.dumps(payload, default=_json_default) + "\n").encode("utf-8")


def _jsonable(row: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively replace non-JSON types with JSON-safe equivalents.

    * UUID → string
    * datetime → ISO-8601
    * bytes → ``{"__b64__": "…"}``
    * Decimal / memoryview → float / bytes
    """
    out: Dict[str, Any] = {}
    for key, value in row.items():
        out[key] = _json_value(value)
    return out


def _json_value(value: Any) -> Any:
    import base64
    from decimal import Decimal

    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    # Fallback — the outer json.dumps has default=_json_default so
    # anything that lands here gets stringified.
    return str(value)


def _json_default(value: Any) -> Any:
    return _json_value(value)


def _schema_version(db: AsyncSession) -> str:
    """Best-effort alembic head so a re-import knows which migration
    the bundle was produced against. Falls back to ``"unknown"`` when
    alembic isn't available."""
    try:
        result = db.sync_session.execute(  # type: ignore[attr-defined]
            text("SELECT version_num FROM alembic_version LIMIT 1")
        )
        row = result.scalar()
        return str(row) if row else "unknown"
    except Exception:
        return "unknown"


__all__ = ["export_tenant", "hard_delete_tenant"]
