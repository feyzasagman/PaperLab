"""workspaces.description: a free-text line about the workspace, set from MCP's create_workspace (M39 F1).

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "description")
