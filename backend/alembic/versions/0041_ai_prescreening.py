"""AI pre-screening (M39 F3): suggestion scores/quotes/flags on hits; question, criteria, settings, run scope on
workspaces.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_search_hits", sa.Column("suggestion_relevance", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "ck_hits_suggestion_relevance", "workspace_search_hits", "suggestion_relevance BETWEEN 1 AND 5"
    )
    op.add_column("workspace_search_hits", sa.Column("suggestion_quotes", postgresql.JSONB(), nullable=True))
    op.add_column("workspace_search_hits", sa.Column("suggestion_confidence", sa.Text(), nullable=True))
    op.add_column("workspace_search_hits", sa.Column("suggestion_flags", postgresql.JSONB(), nullable=True))
    op.add_column("workspace_search_hits", sa.Column("suggestion_prompt_version", sa.SmallInteger(), nullable=True))
    for name in ("research_question", "inclusion_criteria", "exclusion_criteria"):
        op.add_column("workspaces", sa.Column(name, sa.Text(), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("prescreen_after_scheduled_runs", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("workspaces", sa.Column("auto_import_min_relevance", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_auto_import_min_relevance", "workspaces", "auto_import_min_relevance BETWEEN 4 AND 5"
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "suggest_run_id", postgresql.UUID(),
            sa.ForeignKey("workspace_search_runs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.execute("UPDATE workspaces SET inclusion_criteria = screening_criteria")


def downgrade() -> None:
    for name in ("suggest_run_id", "auto_import_min_relevance", "prescreen_after_scheduled_runs",
                 "exclusion_criteria", "inclusion_criteria", "research_question"):
        op.drop_column("workspaces", name)
    for name in ("suggestion_prompt_version", "suggestion_flags", "suggestion_confidence", "suggestion_quotes",
                 "suggestion_relevance"):
        op.drop_column("workspace_search_hits", name)
