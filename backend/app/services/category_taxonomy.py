"""Category taxonomy service — Phase 5B-1.

Action item categories arrive as free-form LLM strings. The user's
direction: start loose, evolve toward a canonical set as patterns repeat.

This service handles three jobs:

1. **Lookup**: given a raw category string, find the canonical name
   (matching against tenant-scoped taxonomy first, then the global default).
2. **Record**: increment the occurrence count on each emission.
3. **Promote**: when a non-canonical candidate crosses the per-tenant
   occurrence threshold, mark it canonical so future prompts can include
   it as a known option.

The taxonomy table is seeded at migration time with global defaults
(``follow_up``, ``commitment_made``, etc.). Tenants can also have
private categories that don't apply globally (e.g. a healthcare tenant
with ``hipaa_review_required``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.models import CategoryTaxonomy

logger = logging.getLogger(__name__)


# Number of occurrences before a candidate category is auto-promoted to
# canonical. Set deliberately low so genuinely useful categories surface
# fast; raise once tenants have substantial volume.
PROMOTION_THRESHOLD = 5


def _normalize(raw: str) -> str:
    """Lowercase + strip + collapse whitespace into single underscores.

    'Follow Up' / 'follow up' / 'follow-up' all → 'follow_up'.
    """
    return "_".join(raw.lower().strip().split()).replace("-", "_")


def lookup_canonical(
    session: Session,
    tenant_id: uuid.UUID,
    raw_category: str,
) -> Optional[str]:
    """Return the canonical name for ``raw_category``, or None if no match.

    Tenant-scoped taxonomy wins over global. Both ``canonical_name`` and
    any of the ``aliases`` count as matches.
    """
    if not raw_category:
        return None
    needle = _normalize(raw_category)

    stmt = (
        select(CategoryTaxonomy)
        .where(
            or_(
                CategoryTaxonomy.tenant_id == tenant_id,
                CategoryTaxonomy.tenant_id.is_(None),
            ),
            CategoryTaxonomy.is_canonical.is_(True),
        )
        .order_by(CategoryTaxonomy.tenant_id.desc().nullslast())
    )
    rows = session.execute(stmt).scalars().all()
    for row in rows:
        if row.canonical_name == needle:
            return row.canonical_name
        aliases = row.aliases or []
        if any(_normalize(a) == needle for a in aliases if isinstance(a, str)):
            return row.canonical_name
    return None


def record_occurrence(
    session: Session,
    tenant_id: uuid.UUID,
    raw_category: str,
) -> Optional[str]:
    """Record an occurrence of ``raw_category`` for the tenant.

    Returns the canonical name in effect (which may be the input itself
    if a new row was created, the matched canonical name if it was
    already known, or the newly-promoted name when this occurrence
    crossed the promotion threshold).

    Never raises — taxonomy bookkeeping must not fail action item
    insertion. Returns None on any error. The work (including its flush)
    runs inside a SAVEPOINT: a rejected write must roll back to here, not
    poison the caller's transaction — record_occurrence runs inside the
    analysis transaction, and a PendingRollbackError there fails the whole
    interaction.
    """
    if not raw_category:
        return None

    try:
        with session.begin_nested():
            result = _record_occurrence(session, tenant_id, _normalize(raw_category))
            # Flush now so DB-side rejections (RLS, constraints) surface
            # while the savepoint can still absorb them.
            session.flush()
        return result
    except Exception:
        logger.exception(
            "category_taxonomy.record_occurrence failed for %r (tenant=%s)",
            raw_category, tenant_id,
        )
        return None


def _record_occurrence(
    session: Session,
    tenant_id: uuid.UUID,
    needle: str,
) -> Optional[str]:
    # 1. Try canonical lookup first (cheap path).
    canonical = lookup_canonical(session, tenant_id, needle)
    if canonical:
        _bump_occurrence(session, tenant_id, canonical)
        return canonical

    # 2. Look for a non-canonical candidate row matching by name OR alias.
    #    Tenant rows first: a tenant copy of a global default must win so
    #    we bump it instead of re-creating it.
    stmt = (
        select(CategoryTaxonomy)
        .where(
            or_(
                CategoryTaxonomy.tenant_id == tenant_id,
                CategoryTaxonomy.tenant_id.is_(None),
            ),
        )
        .order_by(CategoryTaxonomy.tenant_id.desc().nullslast())
    )
    rows = session.execute(stmt).scalars().all()
    match: Optional[CategoryTaxonomy] = None
    for row in rows:
        if row.canonical_name == needle:
            match = row
            break
        aliases = row.aliases or []
        if any(_normalize(a) == needle for a in aliases if isinstance(a, str)):
            match = row
            break

    now = datetime.now(timezone.utc)
    if match is None:
        # 3. Brand-new candidate. Tenant-scoped row, not canonical yet.
        match = CategoryTaxonomy(
            tenant_id=tenant_id,
            canonical_name=needle,
            aliases=[],
            description=None,
            is_canonical=False,
            occurrence_count=1,
            last_seen_at=now,
        )
        session.add(match)
        return needle

    if match.tenant_id is None:
        # Global rows are read-only from tenant sessions (RLS).
        _adopt_global_row(session, tenant_id, match, now)
        return match.canonical_name

    # 4. Existing candidate — bump count, maybe promote.
    match.occurrence_count = (match.occurrence_count or 0) + 1
    match.last_seen_at = now
    if not match.is_canonical and match.occurrence_count >= PROMOTION_THRESHOLD:
        match.is_canonical = True
        match.promoted_at = now
        logger.info(
            "category_taxonomy: promoted %r to canonical (tenant=%s, count=%d)",
            match.canonical_name, tenant_id, match.occurrence_count,
        )
    return match.canonical_name


def _bump_occurrence(
    session: Session,
    tenant_id: uuid.UUID,
    canonical_name: str,
) -> None:
    stmt = select(CategoryTaxonomy).where(
        or_(
            CategoryTaxonomy.tenant_id == tenant_id,
            CategoryTaxonomy.tenant_id.is_(None),
        ),
        CategoryTaxonomy.canonical_name == canonical_name,
        CategoryTaxonomy.is_canonical.is_(True),
    ).order_by(CategoryTaxonomy.tenant_id.desc().nullslast())
    row = session.execute(stmt).scalars().first()
    if row is None:
        return
    if row.tenant_id is None:
        _adopt_global_row(session, tenant_id, row, datetime.now(timezone.utc))
        return
    row.occurrence_count = (row.occurrence_count or 0) + 1
    row.last_seen_at = datetime.now(timezone.utc)


def _adopt_global_row(
    session: Session,
    tenant_id: uuid.UUID,
    global_row: CategoryTaxonomy,
    now: datetime,
) -> None:
    """Count an occurrence against the tenant's own copy of a global row.

    Global (tenant_id IS NULL) rows are writable only through the owner
    connection: the RLS write policy (``backend.app.rls._write_predicate``)
    requires ``tenant_id = app.current_tenant``, so an UPDATE from a tenant
    session raises InsufficientPrivilege. The unique constraint is
    (tenant_id, canonical_name), so a tenant-owned row can sit beside the
    global default — bump that one, creating it on first occurrence.
    """
    stmt = select(CategoryTaxonomy).where(
        CategoryTaxonomy.tenant_id == tenant_id,
        CategoryTaxonomy.canonical_name == global_row.canonical_name,
    )
    row = session.execute(stmt).scalars().first()
    if row is None:
        session.add(
            CategoryTaxonomy(
                tenant_id=tenant_id,
                canonical_name=global_row.canonical_name,
                aliases=list(global_row.aliases or []),
                description=global_row.description,
                is_canonical=global_row.is_canonical,
                occurrence_count=1,
                last_seen_at=now,
            )
        )
        return
    row.occurrence_count = (row.occurrence_count or 0) + 1
    row.last_seen_at = now
    if not row.is_canonical and row.occurrence_count >= PROMOTION_THRESHOLD:
        row.is_canonical = True
        row.promoted_at = now


def list_canonical(session: Session, tenant_id: uuid.UUID) -> List[str]:
    """Return the canonical category names available to a tenant.

    Useful for feeding into the analysis prompt as candidate categories
    so the LLM gravitates toward the established vocabulary instead of
    inventing parallel labels.
    """
    stmt = (
        select(CategoryTaxonomy.canonical_name)
        .where(
            or_(
                CategoryTaxonomy.tenant_id == tenant_id,
                CategoryTaxonomy.tenant_id.is_(None),
            ),
            CategoryTaxonomy.is_canonical.is_(True),
        )
        .distinct()
    )
    return [r for r in session.execute(stmt).scalars().all()]
