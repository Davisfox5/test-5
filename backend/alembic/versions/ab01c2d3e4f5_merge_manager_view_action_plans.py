"""merge manager-view + action-plans heads

Revision ID: ab01c2d3e4f5
Revises: ap_002_step_feedback_and_awaits, ap_001_action_plans
Create Date: 2026-05-28

No-op merge. After the manager-view overhaul (``aa01b2c3d4e5``) and the
action-plans branch (``ap_001_action_plans`` plus its child
``ap_002_step_feedback_and_awaits``) landed in parallel,
``alembic upgrade head`` (singular) failed with "Multiple head revisions
are present" because the two chains never reconverged. This commit
joins the tip of each chain into one head; downstream migrations should
extend from ``ab01c2d3e4f5``.
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "ab01c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = (
    "ap_002_step_feedback_and_awaits",
    "ap_001_action_plans",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
