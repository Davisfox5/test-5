"""Encrypt existing ``webhooks.secret`` rows in place.

Outbound-webhook HMAC secrets were stored plaintext while OAuth tokens
on ``integrations.access_token`` were already Fernet-encrypted. A
leaked Postgres backup could be used to forge our outbound webhook
signatures verbatim. This migration rewrites every existing row using
the same ``backend.app.services.token_crypto.encrypt_token`` wrapper
so the at-rest encryption posture matches the token table.

The dispatcher + management endpoints decrypt on read with the
existing legacy-tolerant ``decrypt_token`` helper, so any rows that
slip through (e.g. a Stripe-customer link inserted via psql while the
deployment is mid-rollout) keep working until the next write.

Revision ID: l9a0b1c2d3e4
Revises: k8f9a0b1c2d3
Create Date: 2026-04-28 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "l9a0b1c2d3e4"
down_revision: Union[str, None] = "k8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Late import — alembic/env.py loads the migration module before
    # the application config is fully wired in some environments. Doing
    # the import here keeps `alembic upgrade --offline` (which never
    # touches the DB and never calls upgrade()) loadable.
    from backend.app.services.token_crypto import (
        _looks_encrypted,
        encrypt_token,
    )

    conn = op.get_bind()

    rows = conn.execute(sa.text("SELECT id, secret FROM webhooks")).fetchall()
    for row in rows:
        secret = row[1]
        if not secret:
            continue
        if _looks_encrypted(secret):
            continue
        ciphertext = encrypt_token(secret)
        if ciphertext == secret:
            # encrypt_token returned the input unchanged (idempotent
            # branch). Skip the write rather than no-op-update.
            continue
        conn.execute(
            sa.text("UPDATE webhooks SET secret = :s WHERE id = :id"),
            {"s": ciphertext, "id": row[0]},
        )


def downgrade() -> None:
    # Reverse: decrypt back to plaintext. Only the operator who has
    # the active TOKEN_ENCRYPTION_KEY can run this; rows that fail
    # decryption are left as-is (legacy-plaintext fallback).
    from backend.app.services.token_crypto import (
        _looks_encrypted,
        decrypt_token,
    )

    conn = op.get_bind()

    rows = conn.execute(sa.text("SELECT id, secret FROM webhooks")).fetchall()
    for row in rows:
        secret = row[1]
        if not secret or not _looks_encrypted(secret):
            continue
        plaintext = decrypt_token(secret)
        if plaintext == secret:
            continue
        conn.execute(
            sa.text("UPDATE webhooks SET secret = :s WHERE id = :id"),
            {"s": plaintext, "id": row[0]},
        )
