"""Make ``api_keys.scopes`` an enforced JSONB column.

The ``scopes`` column already existed on the ``ApiKey`` ORM model with a
default of ``["read:all", "write:all"]`` but was never persisted with a
NOT NULL / server-side default at the DB level — it was decorative and
``auth.py`` ignored its value. This migration:

* Backfills any NULL rows with ``[]`` (an empty scope list — the
  ``require_scope`` dependency that lands alongside this migration treats
  an empty list as "no write access"; admins must regenerate or PATCH
  the key to grant explicit scopes).
* Sets a NOT NULL constraint and a server default of ``'[]'::jsonb`` so
  every future row arrives well-formed.

Existing keys that depended on the old "everything works" behaviour will
fail authorization on writes after this lands. The release runbook calls
this out: tenants should rotate keys with explicit scopes (or pass
``["*"]`` to opt into the legacy "all access" semantics).

Revision ID: q4e5f6a7b8c9
Revises: p3d4e5f6a7b8
Create Date: 2026-04-28 02:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "q4e5f6a7b8c9"
down_revision: Union[str, None] = "p3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Some installs may already have the column from earlier metadata
    # autogeneration runs. Add it conditionally so this migration is safe
    # to apply on either a clean DB or one where the column existed
    # without the constraint we now want.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("api_keys")}

    if "scopes" not in cols:
        op.add_column(
            "api_keys",
            sa.Column(
                "scopes",
                postgresql.JSONB(),
                nullable=True,
            ),
        )

    # Backfill NULL rows with empty list (closed-by-default — admins must
    # grant scopes explicitly when regenerating keys).
    op.execute(
        sa.text(
            "UPDATE api_keys SET scopes = '[]'::jsonb WHERE scopes IS NULL"
        )
    )

    # Tighten the column: NOT NULL + server default.
    op.alter_column(
        "api_keys",
        "scopes",
        existing_type=postgresql.JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )


def downgrade() -> None:
    # Loosen the column back to nullable + drop the server default. We
    # don't drop the column because earlier code paths reference it.
    op.alter_column(
        "api_keys",
        "scopes",
        existing_type=postgresql.JSONB(),
        nullable=True,
        server_default=None,
    )
