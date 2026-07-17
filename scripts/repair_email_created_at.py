#!/usr/bin/env python3
"""Repair created_at on Gmail email Interactions ingested before the
ingest fix that stamps created_at from the message's Date header.

Before that fix, every email Interaction got created_at = ingest time
(the transaction clock), so a backfilled thread renders as N emails at
one identical timestamp in every timeline.

Gmail message ids encode the message's internalDate: the id is a hex
integer whose upper bits are epoch-milliseconds (id >> 20). That lets
us recover the real receive time for every gmail-sourced row from
``provider_message_id`` alone — no API tokens or re-fetch needed.
Verified against live rows: a message with Date ``2026-07-06 13:19``
ingested at ``2026-07-10 00:33`` decodes to ``2026-07-06 13:19:19``.

Graph (Outlook) ids are opaque, so microsoft-sourced rows are skipped.

Only rows whose decoded time differs from created_at by more than
``--threshold-minutes`` (default 5) are touched, so live push-ingested
rows (already ≈ receive time) are left alone.

Run (against whichever DATABASE_URL you export):
    python3 -m scripts.repair_email_created_at            # dry run
    python3 -m scripts.repair_email_created_at --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import psycopg2
except ImportError:
    print("psycopg2 is required", file=sys.stderr)
    raise SystemExit(1)

# Decoded times outside this window mean the id isn't a plain Gmail
# hex id (or the shift assumption broke) — refuse to write garbage.
SANE_MIN = datetime(2004, 4, 1, tzinfo=timezone.utc)  # Gmail launch


def decode_gmail_ts(provider_message_id: str) -> "datetime | None":
    try:
        ms = int(provider_message_id, 16) >> 20
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if dt < SANE_MIN or dt > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return dt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--tenant-id", help="limit to one tenant uuid")
    parser.add_argument("--threshold-minutes", type=int, default=5)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(1)
    url = url.replace("postgresql+asyncpg://", "postgresql://").replace("ssl=require", "sslmode=require")

    conn = psycopg2.connect(url)
    conn.autocommit = False
    cur = conn.cursor()

    where = "channel = 'email' AND source = 'gmail' AND provider_message_id IS NOT NULL"
    params: list = []
    if args.tenant_id:
        where += " AND tenant_id = %s"
        params.append(args.tenant_id)

    cur.execute(
        f"SELECT id, tenant_id, provider_message_id, created_at, subject FROM interactions WHERE {where}",
        params,
    )
    rows = cur.fetchall()
    threshold = timedelta(minutes=args.threshold_minutes)

    fixes = []
    for iid, tenant_id, pmid, created_at, subject in rows:
        decoded = decode_gmail_ts(pmid)
        if decoded is None:
            continue
        if abs(decoded - created_at) <= threshold:
            continue
        fixes.append((iid, tenant_id, created_at, decoded, subject))

    print(f"{len(rows)} gmail email interactions scanned, {len(fixes)} need repair")
    for iid, tenant_id, old, new, subject in fixes:
        print(f"  {iid} tenant={tenant_id} {old:%Y-%m-%d %H:%M:%S} -> {new:%Y-%m-%d %H:%M:%S}  {subject!r}")

    if not args.apply:
        print("dry run — re-run with --apply to write")
        conn.close()
        return

    for iid, _tenant_id, _old, new, _subject in fixes:
        cur.execute("UPDATE interactions SET created_at = %s WHERE id = %s", (new, iid))
    conn.commit()
    conn.close()
    print(f"updated {len(fixes)} rows")


if __name__ == "__main__":
    main()
