"""Workspace tags: names a workspace's papers (and search hits) are grouped under, e.g. by research question.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "workspace_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_workspace_tags_name", "workspace_tags", ["workspace_id", sa.text("lower(name)")], unique=True)
    op.create_table(
        "paper_tags",
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("paper_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", UUID(as_uuid=True), sa.ForeignKey("workspace_tags.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("tag_id", "paper_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "paper_id"],
            ["workspace_papers.workspace_id", "workspace_papers.paper_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_paper_tags_paper", "paper_tags", ["workspace_id", "paper_id"])
    op.create_table(
        "hit_tags",
        sa.Column(
            "hit_id", UUID(as_uuid=True), sa.ForeignKey("workspace_search_hits.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tag_id", UUID(as_uuid=True), sa.ForeignKey("workspace_tags.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("hit_id", "tag_id"),
    )


def downgrade() -> None:
    op.drop_table("hit_tags")
    op.drop_table("paper_tags")
    op.drop_table("workspace_tags")
