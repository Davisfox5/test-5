"""KB vector store (pgvector), chunk index, and pinned cards.

Revision ID: 7a1c3e4b9d12
Revises: 550a40162883
Create Date: 2026-04-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision: str = "7a1c3e4b9d12"
down_revision: Union[str, None] = "550a40162883"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


VECTOR_DIM = 1024  # matches Settings.VOYAGE_EMBED_DIM for voyage-3


def upgrade() -> None:
    # pgvector extension — safe if already installed.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Per-tenant question keyterms (for Deepgram streaming keyterm prompting).
    op.add_column(
        "tenants",
        sa.Column(
            "question_keyterms",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # kb_documents: track when we last embedded the content.
    op.add_column(
        "kb_documents",
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )

    # kb_chunks — one row per embedded slice.
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("doc_id", sa.UUID(), nullable=False),
        sa.Column("chunk_idx", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["doc_id"], ["kb_documents.id"], ondelete="CASCADE"),
    )

    op.create_index("ix_kb_chunks_tenant_id", "kb_chunks", ["tenant_id"])
    op.create_index("ix_kb_chunks_doc_id", "kb_chunks", ["doc_id"])

    # The embedding column — pgvector type isn't shipped with SQLAlchemy core.
    op.execute(f"ALTER TABLE kb_chunks ADD COLUMN embedding vector({VECTOR_DIM})")

    # HNSW index for fast ANN search. Cosine distance matches Voyage's recommendation.
    # lists tuning is pgvector-ivfflat only; HNSW uses m/ef_construction which default OK.
    op.execute(
        "CREATE INDEX ix_kb_chunks_embedding_hnsw ON kb_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # pinned_kb_cards — per-contact pins carried across calls.
    op.create_table(
        "pinned_kb_cards",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("doc_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=False),
        sa.Column("pinned_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "pinned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["doc_id"], ["kb_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["kb_chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pinned_by_user_id"], ["users.id"]),
        sa.UniqueConstraint("contact_id", "chunk_id", name="uq_pinned_contact_chunk"),
    )
    op.create_index("ix_pinned_kb_cards_tenant_id", "pinned_kb_cards", ["tenant_id"])
    op.create_index("ix_pinned_kb_cards_contact_id", "pinned_kb_cards", ["contact_id"])


def downgrade() -> None:
    op.drop_index("ix_pinned_kb_cards_contact_id", table_name="pinned_kb_cards")
    op.drop_index("ix_pinned_kb_cards_tenant_id", table_name="pinned_kb_cards")
    op.drop_table("pinned_kb_cards")

    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")
    op.drop_index("ix_kb_chunks_doc_id", table_name="kb_chunks")
    op.drop_index("ix_kb_chunks_tenant_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")

    op.drop_column("kb_documents", "embedded_at")
    op.drop_column("tenants", "question_keyterms")
    # Leave the pgvector extension installed — it may be used by other consumers.
