"""A model connection's address can be on this machine while the server forwards every request to the cloud.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-16
"""

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "llm_connections", sa.Column("cloud_proxy", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("llm_connections", "cloud_proxy")
