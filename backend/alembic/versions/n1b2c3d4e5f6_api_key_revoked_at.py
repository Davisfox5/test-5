"""Add ``api_keys.revoked_at`` for soft-delete-on-revoke.

Hard-deleting on revoke previously meant "this key existed and was
revoked at X" was unrecoverable from logs alone. Move to a tombstone
column so audit trails survive while authentication still excludes
the row.

Revision ID: n1b2c3d4e5f6
Revises: m0a1b2c3d4e5
Create Date: 2026-04-28 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "n1b2c3d4e5f6"
down_revision: Union[str, None] = "m0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "revoked_at")
