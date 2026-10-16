"""Where a library paper was published, and when enrich_papers last asked Semantic Scholar about it.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-16
"""

import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("papers", sa.Column("published_venue", sa.Text(), nullable=True))
    op.add_column("papers", sa.Column("published_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("papers", "published_checked_at")
    op.drop_column("papers", "published_venue")
