"""merge heads: event_retention_rollup + drop_call_recordings chain

Revision ID: k8f9a0b1c2d3
Revises: a4b5c6d7e8f9, j7e8f9a0b1c2
Create Date: 2026-04-25 14:00:00.000000

After commit 364761a (production-readiness pass 2), two independent
chains diverged from ``f3a4b5c6d7e8``:

- ``a4b5c6d7e8f9`` — event_retention_rollup (single migration)
- ``g4b5c6d7e8f9`` -> ``h5c6d7e8f9a0`` -> ``i6d7e8f9a0b1`` -> ``j7e8f9a0b1c2``
  — drop call_recordings + paralinguistic baselines + CRM deals +
  tenant dataops audit log

The two chains touch disjoint tables, so this is a no-op merge that
unifies the heads. Future migrations extend from this point as a
single head.

Discovered when ``alembic upgrade head`` failed during the first Fly
release_command run.
"""

from typing import Sequence, Union


# revision identifiers
revision: str = "k8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = (
    "a4b5c6d7e8f9",
    "j7e8f9a0b1c2",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
