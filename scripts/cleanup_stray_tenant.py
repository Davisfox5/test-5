#!/usr/bin/env python3
"""Find (and optionally hard-delete) a stray tenant by a user's email.

Background: the SPA's ``POST /trial/signup`` mints a sandbox Tenant + admin
User on signup, with NO allowlist/invite gating. A test login (e.g.
davison@flexonline.net into linda-staging-app) therefore leaves a real
sandbox tenant behind. This script locates that tenant and, only when
explicitly confirmed, deletes it via the same ``hard_delete_tenant`` service
the GDPR endpoint uses (single transaction, cascades child rows).

SAFE BY DEFAULT — dry-run unless ``--execute`` is passed, and ``--execute``
additionally requires ``--tenant-id`` and a ``--confirm-name`` that exactly
matches ``tenants.name``. Deletion is irreversible.

Usage (run from repo root, with DATABASE_URL pointing at the target DB —
e.g. on Fly: ``flyctl ssh console --app linda-staging`` then run it):

    # 1. Inspect — lists every tenant linked to a user with this email + counts
    python scripts/cleanup_stray_tenant.py --email davison@flexonline.net

    # 2. Delete one specific tenant after reading the dry-run output
    python scripts/cleanup_stray_tenant.py \
        --email davison@flexonline.net \
        --tenant-id <uuid-from-step-1> \
        --confirm-name "<exact tenants.name>" \
        --execute
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

# Ensure repo root is importable when invoked as a file path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from backend.app.db import async_session  # noqa: E402
from backend.app.models import (  # noqa: E402
    Integration,
    Interaction,
    Tenant,
    User,
)
from backend.app.services.tenant_dataops import hard_delete_tenant  # noqa: E402


async def _find(db, email: str):
    """Return [(User, Tenant), ...] for every user matching email (ci)."""
    rows = (
        await db.execute(
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(func.lower(User.email) == email.strip().lower())
        )
    ).all()
    return rows


async def _counts(db, tenant_id: uuid.UUID) -> dict:
    async def _count(model) -> int:
        return (
            await db.execute(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            )
        ).scalar_one()

    return {
        "users": await _count(User),
        "interactions": await _count(Interaction),
        "integrations": await _count(Integration),
    }


async def run(args: argparse.Namespace) -> int:
    async with async_session() as db:
        matches = await _find(db, args.email)
        if not matches:
            print(f"No users found with email {args.email!r}. Nothing to do.")
            return 0

        print(f"Found {len(matches)} user(s) for {args.email!r}:\n")
        for user, tenant in matches:
            counts = await _counts(db, tenant.id)
            print(f"  tenant_id    : {tenant.id}")
            print(f"  tenant.name  : {tenant.name!r}")
            print(f"  tenant.slug  : {tenant.slug!r}")
            print(f"  plan_tier    : {tenant.plan_tier}")
            print(f"  created_at   : {tenant.created_at}")
            print(f"  user         : {user.email} (role={user.role}, clerk={user.clerk_user_id})")
            print(f"  row counts   : {counts}")
            print()

        if not args.execute:
            print("Dry run (no --execute). Re-run with --tenant-id + "
                  "--confirm-name + --execute to delete a specific tenant.")
            return 0

        # ── Execute path — strict guards ──────────────────
        if not args.tenant_id or not args.confirm_name:
            print("ERROR: --execute requires both --tenant-id and --confirm-name.")
            return 2
        try:
            target_id = uuid.UUID(args.tenant_id)
        except ValueError:
            print(f"ERROR: --tenant-id {args.tenant_id!r} is not a valid UUID.")
            return 2

        target = next((t for _, t in matches if t.id == target_id), None)
        if target is None:
            print(f"ERROR: tenant {target_id} is not linked to {args.email!r}. "
                  "Refusing to delete a tenant outside the matched set.")
            return 2
        if target.name != args.confirm_name:
            print(f"ERROR: --confirm-name {args.confirm_name!r} != tenants.name "
                  f"{target.name!r}. Refusing to delete.")
            return 2

        print(f"Deleting tenant {target_id} ({target.name!r})…")
        result = await hard_delete_tenant(db, target_id)
        await db.commit()
        print(f"Deleted. Per-table counts: {result.get('deleted')}")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--email", required=True, help="User email to locate the tenant by")
    p.add_argument("--tenant-id", help="Specific tenant UUID to delete (required with --execute)")
    p.add_argument("--confirm-name", help="Must exactly match tenants.name (required with --execute)")
    p.add_argument("--execute", action="store_true", help="Actually delete (default: dry run)")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
