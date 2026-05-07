"""SIPREC initial schema — siprec_sessions table.

Stream 1 of the parallel telephony-integrations build (RFC 7866
SIPREC ingestion for Cisco CUBE, Avaya SBCE, Metaswitch Perimeta).
The table is sibling to ``live_sessions`` rather than an extension
of it; SIPREC carries fields (rs-metadata XML, SBC call id,
negotiated SRTP suite, consent attestation) that have no analogue in
CPaaS sources and we don't want to widen the live-sessions schema
just for this one ingest path.

The corresponding ORM model is
``backend.app.models.SiprecSession``. Bridge logic lives in
``backend.app.services.telephony.siprec.bridge``.

Revision ID: siprec_001_initial
Revises: ab2c3d4e5f6a
Create Date: 2026-05-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "siprec_001_initial"
down_revision: Union[str, None] = "ab2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "siprec_sessions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "live_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("live_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("src_session_id", sa.String(), nullable=False),
        sa.Column("src_call_id", sa.String(), nullable=True),
        sa.Column(
            "src_metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("sdp_crypto_suite", sa.String(), nullable=True),
        sa.Column(
            "is_consent_attested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "src_session_id", name="uq_siprec_sessions_src_session_id"
        ),
    )
    # Tenant-scoped lookups (admin "list my recordings"), provider
    # filters on ops dashboards, and call-id correlation against the
    # customer's CDRs are the three queries this module's API actually
    # runs. Index those.
    op.create_index(
        "ix_siprec_sessions_tenant_id",
        "siprec_sessions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_siprec_sessions_live_session_id",
        "siprec_sessions",
        ["live_session_id"],
    )
    op.create_index(
        "ix_siprec_sessions_integration_id",
        "siprec_sessions",
        ["integration_id"],
    )
    op.create_index(
        "ix_siprec_sessions_src_call_id",
        "siprec_sessions",
        ["src_call_id"],
    )
    op.create_index(
        "ix_siprec_sessions_provider_started",
        "siprec_sessions",
        ["provider", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_siprec_sessions_provider_started", table_name="siprec_sessions")
    op.drop_index("ix_siprec_sessions_src_call_id", table_name="siprec_sessions")
    op.drop_index("ix_siprec_sessions_integration_id", table_name="siprec_sessions")
    op.drop_index("ix_siprec_sessions_live_session_id", table_name="siprec_sessions")
    op.drop_index("ix_siprec_sessions_tenant_id", table_name="siprec_sessions")
    op.drop_table("siprec_sessions")
