"""Hit triage data on external refs: source type, and where a preprint was published.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-16
"""

import sqlalchemy as sa

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

COLUMNS = ("source_type", "published_doi", "published_venue")
CHECKED = "triage_checked_at"  # when enrich_hits last asked Semantic Scholar about the ref


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in COLUMNS:
        op.add_column("external_refs", sa.Column(name, sa.Text(), nullable=True))
    op.add_column("external_refs", sa.Column(CHECKED, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("external_refs", CHECKED)
    for name in reversed(COLUMNS):
        op.drop_column("external_refs", name)
