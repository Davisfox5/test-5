"""Merge phase4 perf indexes with telephony-streams head.

Two heads landed in parallel:
- ``9b8d07fc6ee9`` (telephony streams merge: siprec/uc/teams/audiohook)
- ``a4c5d6e7f8a9`` (phase4 perf indexes)

This migration is a no-op merge so ``alembic upgrade head`` resolves
to a single revision and Fly's release_command stops failing.

Revision ID: c5e6d7f8a9b0
Revises: 9b8d07fc6ee9, a4c5d6e7f8a9
Create Date: 2026-05-10
"""
from typing import Sequence, Union


revision: str = "c5e6d7f8a9b0"
down_revision: Union[str, Sequence[str], None] = ("9b8d07fc6ee9", "a4c5d6e7f8a9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
