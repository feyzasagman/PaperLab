"""saved_searches and workspace_search_runs.saved_search_id (M39 F2).

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "workspace_id", postgresql.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("year_from", sa.Integer(), nullable=True),
        sa.Column("year_to", sa.Integer(), nullable=True),
        sa.Column("schedule", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_run_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_run_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_saved_searches_due", "saved_searches", ["enabled", "next_run_at"])
    op.add_column(
        "workspace_search_runs",
        sa.Column(
            "saved_search_id", postgresql.UUID(), sa.ForeignKey("saved_searches.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.create_index("ix_workspace_search_runs_saved_search_id", "workspace_search_runs", ["saved_search_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_search_runs_saved_search_id", table_name="workspace_search_runs")
    op.drop_column("workspace_search_runs", "saved_search_id")
    op.drop_index("ix_saved_searches_due", table_name="saved_searches")
    op.drop_table("saved_searches")
