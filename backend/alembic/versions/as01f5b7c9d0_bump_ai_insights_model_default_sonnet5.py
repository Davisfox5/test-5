"""bump ai_insights.model column default to claude-sonnet-5

Correction (2026-07-02): ``ai_insights`` is a *legacy* table — the initial
schema migration (``550a40162883``) drops it in ``upgrade()`` and only
recreates it in ``downgrade()``, so no database at or past the initial schema
has it. The audit note that motivated this bump cited the initial schema's
``downgrade()`` section by mistake; the live schema has no model-default
column to bump. The first deploy after this migration merged (once Fly
billing unblocked deploys) failed its release command on staging with
``relation "ai_insights" does not exist``, blocking every later revision in
the chain. The ALTER is therefore guarded on table existence: on every
current database this revision applies as a recorded no-op, keeping the
migration chain intact without renumbering.

Revision ID: as01f5b7c9d0
Revises: ap_003_draft_state
Create Date: 2026-07-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "as01f5b7c9d0"
down_revision = "ap_003_draft_state"
branch_labels = None
depends_on = None


def _ai_insights_exists() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(sa.text("SELECT to_regclass('public.ai_insights')")).scalar()
        is not None
    )


def upgrade() -> None:
    if not _ai_insights_exists():
        return
    op.alter_column(
        "ai_insights",
        "model",
        existing_type=sa.VARCHAR(length=100),
        server_default=sa.text("'claude-sonnet-5'::character varying"),
        existing_nullable=True,
    )


def downgrade() -> None:
    if not _ai_insights_exists():
        return
    op.alter_column(
        "ai_insights",
        "model",
        existing_type=sa.VARCHAR(length=100),
        server_default=sa.text("'claude-sonnet-4-6'::character varying"),
        existing_nullable=True,
    )
