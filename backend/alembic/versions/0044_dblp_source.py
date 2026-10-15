"""Add opt-in DBLP source settings.

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_sources", sa.Column("dblp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("paper_sources", "dblp_enabled")
