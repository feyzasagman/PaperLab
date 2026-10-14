"""Evidence cards (M39 F5): one structured, quote-backed card per paper.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_cards",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("paper_id", postgresql.UUID(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fields", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("provenance", sa.Text(), server_default=sa.text("'llm'"), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.SmallInteger(), nullable=True),
        sa.Column("output_id", postgresql.UUID(), sa.ForeignKey("llm_outputs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("paper_id", name="uq_evidence_cards_paper"),
        sa.CheckConstraint("status IN ('generating', 'ready', 'failed')", name="ck_evidence_cards_status"),
        sa.CheckConstraint("provenance IN ('llm', 'llm_edited')", name="ck_evidence_cards_provenance"),
    )


def downgrade() -> None:
    op.drop_table("evidence_cards")
