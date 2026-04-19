#!/usr/bin/env python3
"""
Targeted scorecard-only runner.

Scores every analyzed interaction for the CallSight demo tenant that doesn't
already have an interaction_scores row. Uses the fixed ScorecardService
(with JSON fence stripping). Cheap — uses Claude Haiku only.
"""

import asyncio
import json
import os
import sys
import time
import uuid

import psycopg2


# ── Load .env ────────────────────────────────────────────────────────────────
_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env):
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DB_URL = os.environ["DATABASE_URL"]

# ── Import services ──────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.services.scorecard_service import ScorecardService


def _segments_to_dicts(transcript_json):
    """Convert stored transcript segments to the dict format ScorecardService expects."""
    segments = []
    for s in transcript_json or []:
        start_ms = s.get("start_ms", 0)
        minutes = (start_ms // 1000) // 60
        seconds = (start_ms // 1000) % 60
        time_str = f"{minutes:02d}:{seconds:02d}"
        segments.append({
            "time": time_str,
            "start_time": time_str,
            "speaker": s.get("speaker_name") or s.get("speaker_id") or "Unknown",
            "text": s.get("text", ""),
        })
    return segments


def _pick_template(title: str, source: str, sales_template: dict, support_template: dict) -> dict:
    """Same heuristic used in analyze_seed.py for consistency."""
    title_lower = title.lower()
    support_keywords = [
        "support", "troubleshoot", "issue", "fix", "bug", "error", "help",
        "outage", "broken", "down", "not working", "crash", "password",
        "integration", "setup", "configuration", "feedback", "complaint",
        "concern", "compliance",
    ]
    if any(kw in title_lower for kw in support_keywords):
        return support_template
    if source == "phone" and "sales" not in title_lower and "discovery" not in title_lower:
        sales_keywords = ["sales", "discovery", "demo", "proposal", "pricing", "contract", "renewal"]
        if any(kw in title_lower for kw in sales_keywords):
            return sales_template
        return support_template
    return sales_template


def _connect_with_retry(max_attempts: int = 5):
    """Connect to the DB, retrying on DNS/connection failures (Neon endpoint wake lag)."""
    import time as _time
    last_err = None
    for attempt in range(max_attempts):
        try:
            return psycopg2.connect(DB_URL, sslmode="require", connect_timeout=15)
        except psycopg2.OperationalError as exc:
            last_err = exc
            wait = 2 * (attempt + 1)
            print(f"  DB connect attempt {attempt+1} failed: {str(exc)[:60]} — retrying in {wait}s", flush=True)
            _time.sleep(wait)
    raise last_err


async def main():
    conn = _connect_with_retry()
    conn.autocommit = False
    cur = conn.cursor()

    # Get tenant
    cur.execute("SELECT id FROM tenants WHERE slug = 'callsight-demo'")
    row = cur.fetchone()
    if not row:
        print("No callsight-demo tenant found.")
        return
    tenant_id = str(row[0])

    # Load scorecard templates
    cur.execute(
        "SELECT id, name, criteria FROM scorecard_templates WHERE tenant_id = %s",
        (tenant_id,),
    )
    templates = {}
    for tmpl_id, tmpl_name, tmpl_criteria in cur.fetchall():
        criteria = tmpl_criteria if isinstance(tmpl_criteria, list) else json.loads(tmpl_criteria)
        templates[tmpl_name] = {
            "id": str(tmpl_id),
            "name": tmpl_name,
            "criteria": criteria,
        }
    sales_template = templates.get("Sales QA", {})
    support_template = templates.get("Support QA", {})

    # Find analyzed interactions that don't yet have a score
    cur.execute(
        """
        SELECT i.id, i.title, i.source, i.transcript, i.insights
        FROM interactions i
        LEFT JOIN interaction_scores s ON s.interaction_id = i.id
        WHERE i.tenant_id = %s
          AND i.status = 'analyzed'
          AND s.id IS NULL
        ORDER BY i.created_at
        """,
        (tenant_id,),
    )
    rows = cur.fetchall()
    print(f"Found {len(rows)} analyzed interactions without scorecards.")

    scorecard = ScorecardService()
    succeeded = 0
    failed = 0

    for idx, (interaction_id, title, source, transcript_json, insights_json) in enumerate(rows, 1):
        template = _pick_template(title, source or "", sales_template, support_template)
        if not template:
            print(f"[{idx}/{len(rows)}] skip — no template: {title}")
            continue

        print(f"[{idx}/{len(rows)}] scoring: {title} (template: {template.get('name')})...", end=" ", flush=True)

        segments = _segments_to_dicts(
            transcript_json if isinstance(transcript_json, list) else (json.loads(transcript_json) if transcript_json else [])
        )
        insights = insights_json if isinstance(insights_json, dict) else (json.loads(insights_json) if insights_json else {})

        try:
            result = await scorecard.score(segments, template, insights)
            if not result.get("criterion_scores"):
                print(f"empty result — {result.get('error','')[:60]}")
                failed += 1
                continue

            # Use a fresh connection if the previous one dropped (DNS/timeout hiccup).
            try:
                cur.execute("SELECT 1")
            except Exception:
                print(" (reconnecting...)", end=" ", flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = _connect_with_retry()
                cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO interaction_scores
                    (id, interaction_id, template_id, tenant_id, total_score, criterion_scores)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    interaction_id,
                    template["id"],
                    tenant_id,
                    result.get("total_score", 0),
                    json.dumps(result.get("criterion_scores", [])),
                ),
            )
            conn.commit()
            print(f"qa: {result.get('total_score', 0):.1f}")
            succeeded += 1
        except Exception as exc:
            print(f"FAILED: {exc}")
            try:
                conn.rollback()
            except Exception:
                # Connection may be dead — reopen for next iteration.
                try:
                    conn.close()
                except Exception:
                    pass
                conn = _connect_with_retry()
                cur = conn.cursor()
            failed += 1

        # Gentle rate limit
        await asyncio.sleep(1.0)

    print(f"\nDone. Succeeded: {succeeded}, Failed: {failed}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
