"""Make ``integrations.user_id`` nullable.

The OAuth upsert previously fell back to ``user_id = tenant_id`` when no
authorizing user was on the state payload. That's a UUID type-collision
(tenant ids aren't valid ``users.id`` rows) and would FK-violate as
soon as anyone enabled the constraint cleanly. Tenant-wide integrations
should sit on the row with ``user_id IS NULL``.

Revision ID: m0a1b2c3d4e5
Revises: l9a0b1c2d3e4
Create Date: 2026-04-28 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "m0a1b2c3d4e5"
down_revision: Union[str, None] = "l9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "integrations",
        "user_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "integrations",
        "user_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
