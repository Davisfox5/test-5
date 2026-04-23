"""merge scoring and continuous-ai heads

Revision ID: b58a9f3c2e11
Revises: 7a1c3e2d9f40, c0a17e1bf001
Create Date: 2026-04-17 14:00:00.000000

No-op merge of the two alembic branches that diverged from the initial
schema: ``7a1c3e2d9f40`` (scoring & orchestrator) and ``c0a17e1bf001``
(continuous-AI-improvement, campaigns, email).  Future migrations extend
from this point as a single head.
"""

from typing import Sequence, Union


# revision identifiers
revision: str = "b58a9f3c2e11"
down_revision: Union[str, Sequence[str], None] = ("7a1c3e2d9f40", "c0a17e1bf001")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
