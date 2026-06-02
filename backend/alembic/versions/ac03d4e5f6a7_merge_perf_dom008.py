"""Merge perf-audit head with dom_008 support-case embedding head.

After the perf-cost-audit-pass PR landed the chain
``ab01c2d3e4f5 -> aa02b3c4d5e6 -> ab02c3d4e5f6``, and the cross-customer
trend-detection PR independently landed ``dom_008_support_case_embedding``
as a sibling head, ``alembic upgrade head`` (singular) failed with
"Multiple head revisions are present". This no-op merge re-converges
both tips into one head; downstream migrations should extend from
``ac03d4e5f6a7``.

Revision ID: ac03d4e5f6a7
Revises: ab02c3d4e5f6, dom_008_support_case_embedding
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "ac03d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = (
    "ab02c3d4e5f6",
    "dom_008_support_case_embedding",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
