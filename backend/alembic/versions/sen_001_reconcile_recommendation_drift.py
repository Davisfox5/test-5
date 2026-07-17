"""Reconcile live-schema drift behind the Sentry LINDA-STAGING-2T/1R sweep.

Two pieces of real model↔schema drift, verified against the live staging
DB with an autogenerate compare (2026-07-16):

1. ``ck_manager_recommendations_category`` still only allows the four
   original sales categories from ``aa01b2c3d4e5``. The builder gained
   CS / IT-support / predictive categories (``dom_002`` era and later)
   and the cohort/trend detectors write more still — every one of those
   INSERTs dies with CheckViolation, which is what poisoned the session
   behind LINDA-STAGING-2T. Recreate the constraint from the full
   category universe (mirrored by ``models.MANAGER_RECOMMENDATION_CATEGORIES``
   and guarded by ``tests/test_manager_recommendation_categories.py``).

2. ``manager_alerts.kind`` is VARCHAR(32) in the DB but String(48) in
   the model — a detector emitting a longer kind would 500 with
   StringDataRightTruncation. Widen to 48 (metadata-only ALTER).

The rest of the autogenerate diff (~130 entries) was inspected and is
deliberate, not drift: this repo declares perf/composite indexes and
unique constraints in migrations without mirroring them on the ORM
models (so autogenerate proposes dropping them), NUMERIC-vs-Float and
TEXT-vs-VARCHAR column pairs are equivalent at runtime, and the
``kb_chunks.embedding`` pgvector column reflects as an unknown type.
None of those can produce runtime errors; touching them from an
autogenerate dump risks dropping load-bearing indexes.

Revision ID: sen_001_reconcile_recommendation_drift
Revises: out_002_outreach_links
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "sen_001_reconcile_recommendation_drift"
down_revision = "out_002_outreach_links"
branch_labels = None
depends_on = None


# The full category universe across every writer:
# manager_recommendation_builder._VALID_CATEGORIES_BY_DOMAIN (all
# domains), cohort_recommendations, support_trend_detector,
# cs_trend_detector, orchestrator. Mirrors
# ``models.MANAGER_RECOMMENDATION_CATEGORIES`` — adding a category there
# requires a follow-up migration extending this list.
_CATEGORIES = (
    # sales
    "coach_rep",
    "run_campaign",
    "outreach_at_risk_customer",
    "promote_winning_script",
    "prevent_lead_stall",
    "address_sales_trend",
    # customer service
    "schedule_qbr",
    "flag_renewal_risk",
    "assign_expansion_play",
    "coach_csm",
    "prevent_no_touch_churn",
    "proactive_outreach_repeat_support",
    "address_cs_trend",
    # IT support
    "update_kb_article",
    "route_to_specialist",
    "coach_support_agent",
    "escalate_recurring_issue",
    "address_recurring_issue",
)

_LEGACY_CATEGORIES = (
    "coach_rep",
    "run_campaign",
    "outreach_at_risk_customer",
    "promote_winning_script",
)


def _check_sql(categories) -> str:
    return "category IN (" + ", ".join("'%s'" % c for c in categories) + ")"


def upgrade() -> None:
    op.drop_constraint(
        "ck_manager_recommendations_category",
        "manager_recommendations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_manager_recommendations_category",
        "manager_recommendations",
        _check_sql(_CATEGORIES),
    )
    op.alter_column(
        "manager_alerts",
        "kind",
        type_=sa.String(length=48),
        existing_nullable=False,
    )


def downgrade() -> None:
    # NOTE: only safe on a DB that has no rows using the newer
    # categories / no kind values longer than 32 chars.
    op.alter_column(
        "manager_alerts",
        "kind",
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.drop_constraint(
        "ck_manager_recommendations_category",
        "manager_recommendations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_manager_recommendations_category",
        "manager_recommendations",
        _check_sql(_LEGACY_CATEGORIES),
    )
