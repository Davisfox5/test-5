"""Purge customer rows with no referencing interactions.

Cleans up orphan ``customers`` rows produced by the dedupe race that
PR #70 fixed forward but couldn't retroactively merge. The Phase 3.5
diagnostic runs on 2026-05-03/04 each created fresh duplicate
``Acme Logistics`` / ``Maple River Health`` / ``Northstar Capital`` /
``Riverbank Manufacturing`` rows because the score fuser ranked the
"create new" candidate above existing-row matches when no domain was
available to corroborate either side.

After PR #70's fix, new resolutions auto-link to the canonical row.
But the staging tenant ended up with ~16 orphan customer rows whose
referencing interactions had been deleted (or never existed) — they
clutter the SPA's customers list and aren't safe to remove via the
``DELETE /customers/{id}`` endpoint because of the FK chain on
``contacts.customer_id``.

This migration purges any customer that has zero rows in
``interactions`` referencing it. Cleanup chain:

1. Delete contacts attached to those orphan customers (they have no
   interactions either, by transitivity — if a contact had an
   interaction, the interaction would reference the customer too).
2. Delete the orphan customers. ``customer_owners`` and
   ``customer_outcome_events`` cascade automatically per their FK
   ``ON DELETE CASCADE``.

Forward-only. The orphans were never legitimate data; restoring them
would only resurrect the bug they came from.

Revision ID: w0e1f2a3b4c5
Revises: v9d0e1f2a3b4
Create Date: 2026-05-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "w0e1f2a3b4c5"
down_revision: Union[str, None] = "v9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Delete contacts attached to orphan customers. A contact
    # whose customer has zero interactions necessarily has zero
    # interactions of its own (an interaction with a contact_id also
    # carries that customer_id), so this never strands real data.
    op.execute(
        text(
            """
            DELETE FROM contacts
            WHERE customer_id IS NOT NULL
              AND customer_id IN (
                SELECT c.id
                FROM customers c
                LEFT JOIN interactions i ON i.customer_id = c.id
                WHERE i.id IS NULL
              )
            """
        )
    )

    # Step 2: Delete the orphan customers themselves. ON DELETE CASCADE
    # on customer_owners + customer_outcome_events handles those tables;
    # interactions.customer_id is ON DELETE SET NULL but every orphan
    # by definition has zero interactions, so the SET NULL doesn't
    # touch anything in practice.
    op.execute(
        text(
            """
            DELETE FROM customers
            WHERE id IN (
                SELECT c.id
                FROM customers c
                LEFT JOIN interactions i ON i.customer_id = c.id
                WHERE i.id IS NULL
            )
            """
        )
    )


def downgrade() -> None:
    # Forward-only — these rows were never legitimate data.
    pass
