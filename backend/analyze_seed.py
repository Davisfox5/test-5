#!/usr/bin/env python3
"""
CallSight AI — Analyze all seeded interactions with the real AI pipeline.

Runs triage, analysis, scorecard scoring, and snippet extraction on every
interaction with status='transcribed' in the callsight-demo tenant.

Usage:
    python -m backend.analyze_seed      # from project root
    python backend/analyze_seed.py      # also works
"""

import asyncio
import json
import logging
import os
import sys
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

# ── Ensure project root is on sys.path ──────────────────────────────────────

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

# ── Load .env manually (for DATABASE_URL and ANTHROPIC_API_KEY) ─────────────

_env_path = os.path.join(_PROJECT_ROOT, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

DB_URL = os.environ["DATABASE_URL"]

# ── Import services ─────────────────────────────────────────────────────────

from backend.app.services.transcription import Segment  # noqa: E402
from backend.app.services.call_metrics import CallMetricsService  # noqa: E402
from backend.app.services.transcript_compressor import TranscriptCompressor  # noqa: E402
from backend.app.services.triage_service import TriageService  # noqa: E402
from backend.app.services.ai_analysis import AIAnalysisService  # noqa: E402
from backend.app.services.scorecard_service import ScorecardService  # noqa: E402
from backend.app.services.snippet_service import SnippetService  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Helpers ─────────────────────────────────────────────────────────────────


def segments_from_jsonb(transcript_jsonb: List[dict]) -> List[Segment]:
    """Convert JSONB transcript segments to Segment dataclass instances."""
    segments: List[Segment] = []
    for seg in transcript_jsonb:
        start_sec = seg.get("start_ms", 0) / 1000.0
        end_sec = seg.get("end_ms", 0) / 1000.0
        segments.append(Segment(
            start=start_sec,
            end=end_sec,
            text=seg.get("text", ""),
            speaker_id=seg.get("speaker_id"),
            confidence=0.97,
        ))
    return segments


def segments_to_analysis_format(segments: List[Segment]) -> List[Dict[str, Any]]:
    """Convert Segment objects to dicts expected by AIAnalysisService.analyze()."""
    result: List[Dict[str, Any]] = []
    for seg in segments:
        minutes = int(seg.start // 60)
        seconds = int(seg.start % 60)
        time_str = f"{minutes:02d}:{seconds:02d}"
        result.append({
            "time": time_str,
            "start_time": time_str,
            "speaker": seg.speaker_id or "Unknown",
            "text": seg.text,
        })
    return result


def compressed_to_text(segments: List[Segment]) -> str:
    """Build a plain-text transcript string from compressed segments."""
    lines: List[str] = []
    for seg in segments:
        speaker = seg.speaker_id or "Unknown"
        lines.append(f"{speaker}: {seg.text}")
    return "\n".join(lines)


# ── Main analysis pipeline ─────────────────────────────────────────────────


async def analyze_all() -> None:
    conn = psycopg2.connect(DB_URL, sslmode="require")
    psycopg2.extras.register_uuid()

    try:
        cur = conn.cursor()

        # Get tenant id
        cur.execute("SELECT id FROM tenants WHERE slug = 'callsight-demo'")
        row = cur.fetchone()
        if not row:
            print("ERROR: Tenant 'callsight-demo' not found. Run backend/seed.py first.")
            return
        tenant_id = str(row[0])

        # Get scorecard templates
        cur.execute(
            "SELECT id, name, criteria FROM scorecard_templates WHERE tenant_id = %s",
            (tenant_id,),
        )
        templates = {}
        for tmpl_row in cur.fetchall():
            tmpl_id, tmpl_name, tmpl_criteria = tmpl_row
            criteria = tmpl_criteria if isinstance(tmpl_criteria, list) else json.loads(tmpl_criteria)
            templates[tmpl_name] = {
                "id": str(tmpl_id),
                "name": tmpl_name,
                "criteria": criteria,
            }

        sales_template = templates.get("Sales QA", {})
        support_template = templates.get("Support QA", {})

        # Load all transcribed interactions
        cur.execute(
            """
            SELECT i.id, i.title, i.transcript, i.duration_seconds,
                   i.agent_id, i.contact_id, i.source
            FROM interactions i
            WHERE i.tenant_id = %s AND i.status = 'transcribed'
            ORDER BY i.created_at
            """,
            (tenant_id,),
        )
        interactions = cur.fetchall()
        total = len(interactions)

        if total == 0:
            print("No interactions with status='transcribed' found. Nothing to analyze.")
            return

        print(f"Found {total} interactions to analyze.\n")

        # Initialize services
        metrics_svc = CallMetricsService()
        compressor = TranscriptCompressor()
        triage_svc = TriageService()
        analysis_svc = AIAnalysisService()
        scorecard_svc = ScorecardService()
        snippet_svc = SnippetService()

        succeeded = 0
        failed = 0

        for idx, row in enumerate(interactions):
            interaction_id = str(row[0])
            title = row[1] or "Untitled"
            transcript_jsonb = row[2] if isinstance(row[2], list) else json.loads(row[2])
            duration = row[3] or 0
            agent_id = str(row[4]) if row[4] else None
            contact_id = str(row[5]) if row[5] else None
            source = row[6] or "phone"

            print(f"[{idx + 1}/{total}] Analyzing: {title}...", end=" ", flush=True)

            try:
                # Convert to Segment objects
                segments = segments_from_jsonb(transcript_jsonb)

                # 1. Compute call metrics (no LLM)
                call_metrics = metrics_svc.compute(segments, agent_speaker_ids=["agent"])

                # 2. Compress transcript for LLM
                compressed = compressor.compress(segments)
                compressed_text = compressed_to_text(compressed)

                # 3. Triage — score complexity (Haiku API call)
                metadata = {
                    "channel": "voice",
                    "duration": duration,
                    "caller_info": title,
                }
                triage_result = await triage_svc.score_complexity(compressed_text, metadata)
                complexity_score = float(triage_result.get("complexity_score", 0.5))
                analysis_tier = triage_result.get("recommended_tier", "sonnet")

                # 4. Deep analysis (Haiku or Sonnet based on triage)
                analysis_segments = segments_to_analysis_format(compressed)
                insights = await analysis_svc.analyze(
                    analysis_segments,
                    tier=analysis_tier,
                    triage_result=triage_result,
                )

                # 5. Scorecard scoring
                # Use Sales QA for sales-related calls, Support QA for IT/CS
                template = sales_template
                if any(kw in title.lower() for kw in [
                    "support", "troubleshoot", "issue", "fix", "bug", "error",
                    "ticket", "incident", "outage", "config", "webhook",
                    "twilio", "api", "sync", "migration", "upgrade",
                    "billing", "refund", "cancel", "complaint", "export",
                    "password", "permission", "access", "sso", "mfa",
                ]):
                    template = support_template
                elif source in ("phone",) and "sales" not in title.lower() and "discovery" not in title.lower():
                    # Default phone calls to support unless clearly sales
                    if any(kw in title.lower() for kw in [
                        "demo", "pricing", "enterprise", "proposal",
                        "negotiation", "close", "contract", "pilot",
                    ]):
                        template = sales_template
                    else:
                        template = support_template

                scorecard_result = await scorecard_svc.score(
                    analysis_segments,
                    template,
                    insights,
                )

                # 6. Snippets
                snippets = snippet_svc.identify_notable_segments(
                    insights,
                    agent_id=agent_id or "",
                    tenant_id=tenant_id,
                )

                # ── Write results to DB ─────────────────────────────────
                update_cur = conn.cursor()

                # If the AI analysis had a JSON parse error, mark row as failed
                # (rather than analyzed) so we can easily retry just these.
                final_status = "failed" if insights.get("error") else "analyzed"

                update_cur.execute(
                    """
                    UPDATE interactions
                    SET status = %s,
                        insights = %s,
                        call_metrics = %s,
                        complexity_score = %s,
                        analysis_tier = %s
                    WHERE id = %s
                    """,
                    (
                        final_status,
                        json.dumps(insights),
                        json.dumps(call_metrics),
                        complexity_score,
                        analysis_tier,
                        interaction_id,
                    ),
                )

                if final_status == "failed":
                    print(f"  PARSE FAILED: {title} — {insights.get('error', '')[:80]}")
                    continue  # skip inserting action_items/snippets/scores for failed rows

                # Insert action items
                for item in insights.get("action_items", []):
                    update_cur.execute(
                        """
                        INSERT INTO action_items
                            (id, interaction_id, tenant_id, title, description,
                             category, priority, status, automation_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            interaction_id,
                            tenant_id,
                            item.get("title", "Untitled"),
                            item.get("suggested_email_draft") or item.get("description", ""),
                            item.get("category", "general"),
                            item.get("priority", "medium"),
                            "pending",
                            "pending",
                        ),
                    )

                # Insert scorecard scores
                template_id = template.get("id", "")
                if template_id and scorecard_result.get("criterion_scores"):
                    update_cur.execute(
                        """
                        INSERT INTO interaction_scores
                            (id, interaction_id, template_id, tenant_id,
                             total_score, criterion_scores)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            interaction_id,
                            template_id,
                            tenant_id,
                            scorecard_result.get("total_score", 0),
                            json.dumps(scorecard_result.get("criterion_scores", [])),
                        ),
                    )

                # Insert snippets
                for snip in snippets:
                    # Parse start_time / end_time strings to floats
                    start_f = _parse_time_to_seconds(snip.get("start_time", ""))
                    end_f = _parse_time_to_seconds(snip.get("end_time", ""))
                    if end_f <= start_f:
                        end_f = start_f + 10.0  # default 10s snippet

                    update_cur.execute(
                        """
                        INSERT INTO interaction_snippets
                            (id, interaction_id, tenant_id, start_time, end_time,
                             snippet_type, quality, title, description,
                             transcript_excerpt, tags, in_library, library_category)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            snip.get("id", str(uuid.uuid4())),
                            interaction_id,
                            tenant_id,
                            start_f,
                            end_f,
                            snip.get("snippet_type", "notable"),
                            snip.get("quality", "neutral"),
                            snip.get("title", ""),
                            snip.get("description", ""),
                            json.dumps(snip.get("transcript_excerpt", [])),
                            json.dumps(snip.get("tags", [])),
                            snip.get("in_library", False),
                            snip.get("library_category"),
                        ),
                    )

                conn.commit()

                total_score = scorecard_result.get("total_score", 0)
                print(f"done (tier: {analysis_tier}, score: {complexity_score:.2f}, "
                      f"qa: {total_score})")
                succeeded += 1

            except Exception as exc:
                conn.rollback()
                print(f"FAILED: {exc}")
                logger.error("Error analyzing %s: %s", title, traceback.format_exc())
                failed += 1

            # Rate-limit delay between interactions
            if idx < total - 1:
                time.sleep(1.5)

        # ── Summary ─────────────────────────────────────────────────────
        print()
        print("=" * 60)
        print(f"Analysis complete: {succeeded} succeeded, {failed} failed "
              f"(out of {total})")
        print("=" * 60)

    finally:
        conn.close()


def _parse_time_to_seconds(time_str: str) -> float:
    """Parse a time string like '02:34' or '1:02:34' to seconds.

    Returns 0.0 if unparseable.
    """
    if not time_str:
        return 0.0
    try:
        parts = time_str.split(":")
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        else:
            return float(time_str)
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    asyncio.run(analyze_all())
