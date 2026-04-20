#!/usr/bin/env python3
"""
LINDA — Seed script for the new interactions schema.

Reads conversation data from the three legacy seed files (seed_sales.py,
seed_it.py, seed_cs.py) and inserts everything into the new schema:
tenants, users, customers, contacts, interactions, scorecard_templates,
and api_keys.

Usage:
    python -m backend.seed          # from project root
    python backend/seed.py          # also works
"""

import hashlib
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ── Load .env manually ──────────────────────────────────────────────────────

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path = os.path.join(_PROJECT_ROOT, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

DB_URL = os.environ["DATABASE_URL"]

# ── Import data from legacy seed files ──────────────────────────────────────

sys.path.insert(0, _PROJECT_ROOT)
from seed_sales import SALES_CALLS, AGENTS  # noqa: E402
from seed_it import IT_CALLS  # noqa: E402
from seed_cs import CS_CALLS  # noqa: E402

# ── Helpers ─────────────────────────────────────────────────────────────────


def new_id() -> str:
    return str(uuid.uuid4())


def ms_from_words(text: str, start_ms: int) -> int:
    """Estimate end_ms from word count at ~140 wpm."""
    words = len(text.split())
    duration_ms = int((words / 140) * 60 * 1000)
    return start_ms + max(duration_ms, 1500)


def days_ago(n: int) -> datetime:
    return datetime.utcnow() - timedelta(days=n)


def get_agent_email(call: dict) -> str:
    """Extract agent email — seed_sales/seed_cs use 'agent', seed_it uses 'agent_email'."""
    return call.get("agent") or call.get("agent_email", "")


def build_transcript_jsonb(segments: List[dict]) -> List[dict]:
    """Convert legacy segment dicts to the JSONB transcript format.

    Each segment gets start_ms / end_ms computed from text length.
    """
    result = []
    cursor_ms = 0
    for seg in segments:
        start_ms = cursor_ms
        end_ms = ms_from_words(seg["text"], start_ms)
        result.append({
            "speaker_id": seg.get("speaker_id", "unknown"),
            "speaker_name": seg.get("speaker_name", "Unknown"),
            "text": seg["text"],
            "sentiment": seg.get("sentiment", "neutral"),
            "start_ms": start_ms,
            "end_ms": end_ms,
        })
        cursor_ms = end_ms + 200  # small gap between turns
    return result


# ── Main seed logic ────────────────────────────────────────────────────────


def seed() -> None:
    conn = psycopg2.connect(DB_URL, sslmode="require")
    conn.autocommit = False
    psycopg2.extras.register_uuid()

    try:
        cur = conn.cursor()

        # ── 1. Tenant ───────────────────────────────────────────────────
        tenant_id = new_id()
        cur.execute(
            """
            INSERT INTO tenants (id, name, slug)
            VALUES (%s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (tenant_id, "Linda Demo", "linda-demo"),
        )
        tenant_id = str(cur.fetchone()[0])
        print(f"Tenant: {tenant_id}")

        # ── 2. Users (agents) ───────────────────────────────────────────
        user_map: Dict[str, str] = {}  # email -> user_id
        for agent in AGENTS:
            # Check if user already exists for this tenant
            cur.execute(
                "SELECT id FROM users WHERE email = %s AND tenant_id = %s",
                (agent["email"], tenant_id),
            )
            existing = cur.fetchone()
            if existing:
                user_map[agent["email"]] = str(existing[0])
            else:
                user_id = new_id()
                role = "agent" if agent["role"] == "member" else agent["role"]
                cur.execute(
                    """
                    INSERT INTO users (id, tenant_id, email, name, role)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (user_id, tenant_id, agent["email"], agent["full_name"], role),
                )
                user_map[agent["email"]] = str(cur.fetchone()[0])
        print(f"Users: {len(user_map)}")

        # ── 3. Companies and Contacts ───────────────────────────────────
        all_calls = []
        for call in SALES_CALLS:
            all_calls.append(("sales", call))
        for call in IT_CALLS:
            all_calls.append(("it", call))
        for call in CS_CALLS:
            all_calls.append(("cs", call))

        customer_map: Dict[str, str] = {}  # customer business name -> customer_id
        contact_map: Dict[str, str] = {}  # contact person name -> contact_id

        for call_type, call in all_calls:
            # In the seed JSON: "customer_company" is the customer's business
            # (the Customer row); "customer_name" is the contact person.
            customer_biz_name = call.get("customer_company", "")
            contact_person_name = call.get("customer_name", "")

            # Create customer if not seen
            if customer_biz_name and customer_biz_name not in customer_map:
                customer_id = new_id()
                cur.execute(
                    """
                    INSERT INTO customers (id, tenant_id, name, metadata)
                    VALUES (%s, %s, %s, '{}'::jsonb)
                    RETURNING id
                    """,
                    (customer_id, tenant_id, customer_biz_name),
                )
                customer_map[customer_biz_name] = str(cur.fetchone()[0])

            # Create contact if not seen
            if contact_person_name and contact_person_name not in contact_map:
                contact_id = new_id()
                cust_id = customer_map.get(customer_biz_name)
                cur.execute(
                    """
                    INSERT INTO contacts (id, tenant_id, name, customer_id, interaction_count, sentiment_trend, metadata)
                    VALUES (%s, %s, %s, %s, 0, '[]'::jsonb, '{}'::jsonb)
                    RETURNING id
                    """,
                    (contact_id, tenant_id, contact_person_name, cust_id),
                )
                contact_map[contact_person_name] = str(cur.fetchone()[0])

        print(f"Customers: {len(customer_map)}")
        print(f"Contacts: {len(contact_map)}")

        # ── 4. Scorecard Templates ──────────────────────────────────────
        sales_qa_id = new_id()
        sales_qa_criteria = [
            {"name": "Greeting", "weight": 10},
            {"name": "Needs Discovery", "weight": 20},
            {"name": "Value Proposition", "weight": 20},
            {"name": "Objection Handling", "weight": 20},
            {"name": "Closing", "weight": 15},
            {"name": "Compliance", "weight": 15},
        ]
        cur.execute(
            """
            INSERT INTO scorecard_templates (id, tenant_id, name, criteria, is_default)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (sales_qa_id, tenant_id, "Sales QA", json.dumps(sales_qa_criteria), True),
        )
        sales_qa_id = str(cur.fetchone()[0])

        support_qa_id = new_id()
        support_qa_criteria = [
            {"name": "Greeting", "weight": 10},
            {"name": "Problem Identification", "weight": 25},
            {"name": "Technical Knowledge", "weight": 20},
            {"name": "Resolution", "weight": 25},
            {"name": "Follow-up", "weight": 10},
            {"name": "Empathy", "weight": 10},
        ]
        cur.execute(
            """
            INSERT INTO scorecard_templates (id, tenant_id, name, criteria, is_default)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (support_qa_id, tenant_id, "Support QA", json.dumps(support_qa_criteria), False),
        )
        support_qa_id = str(cur.fetchone()[0])
        print("Scorecard templates: 2 (Sales QA, Support QA)")

        # ── 5. Interactions ─────────────────────────────────────────────
        interaction_count = 0
        for call_type, call in all_calls:
            agent_email = get_agent_email(call)
            agent_id = user_map.get(agent_email)
            customer_name = call.get("customer_name", "")
            contact_id = contact_map.get(customer_name)

            transcript_jsonb = build_transcript_jsonb(call.get("segments", []))
            created_at = days_ago(call.get("days_ago", 1))

            interaction_id = new_id()
            cur.execute(
                """
                INSERT INTO interactions
                    (id, tenant_id, agent_id, contact_id, channel, source,
                     direction, title, transcript, duration_seconds,
                     status, engine, insights, call_metrics, participants,
                     pii_redacted, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, false, %s)
                """,
                (
                    interaction_id,
                    tenant_id,
                    agent_id,
                    contact_id,
                    "voice",
                    call.get("source", "phone"),
                    "inbound",
                    call.get("title", "Untitled Call"),
                    json.dumps(transcript_jsonb),
                    call.get("duration_secs", 0),
                    "transcribed",
                    "deepgram",
                    created_at,
                ),
            )
            interaction_count += 1

        print(f"Interactions: {interaction_count}")

        # ── 6. API Key ──────────────────────────────────────────────────
        plaintext_key = "csk_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
        api_key_id = new_id()
        cur.execute(
            """
            INSERT INTO api_keys (id, tenant_id, key_hash, name, scopes)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (api_key_id, tenant_id, key_hash, "Demo Seed Key", json.dumps(["read:all", "write:all"])),
        )

        conn.commit()

        print()
        print("=" * 60)
        print(f"Created {interaction_count} interactions, "
              f"{len(contact_map)} contacts, "
              f"{len(customer_map)} customers.")
        print(f"API Key: {plaintext_key}")
        print("=" * 60)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
