"""merge heads after post-consolidation: recording_retention, three_tier_plans, attachments, outcomes_hardening

Revision ID: d1f2a3b4c5e6
Revises: 9d3c7f1b4e26, b2c3d4e5f6a7, a2e8f3b71c05, c9e1d4b7f3a8
Create Date: 2026-04-21 06:00:00.000000

No-op merge that unifies the four alembic heads that arrived via
independent feature branches:

- ``9d3c7f1b4e26`` — recording retention (transcription branch)
- ``b2c3d4e5f6a7`` — three-tier plans + trials + demo capture (brainstorm)
- ``a2e8f3b71c05`` — email attachments + HTML + BCC (pbMCc)
- ``c9e1d4b7f3a8`` — outcomes hardening (fix-ai)

Future migrations extend from this point as a single head.
"""

from typing import Sequence, Union


# revision identifiers
revision: str = "d1f2a3b4c5e6"
down_revision: Union[str, Sequence[str], None] = (
    "9d3c7f1b4e26",
    "b2c3d4e5f6a7",
    "a2e8f3b71c05",
    "c9e1d4b7f3a8",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
