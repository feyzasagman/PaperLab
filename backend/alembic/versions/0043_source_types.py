"""Source type, peer review and the published version of a preprint (M39 F6).

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

SOURCE_TYPES = "'journal_article','proceedings','book','chapter','preprint','magazine','thesis','other'"

# Mirrors app.core.source_types (OpenAlex mapping + the arXiv-DOI rule); no network. Only fills rows still NULL.
BACKFILL_SQL = """
UPDATE papers SET source_type = CASE
    WHEN lower(doi) LIKE '10.48550/arxiv.%' THEN 'preprint'
    WHEN lower(type) IN ('article', 'review') THEN 'journal_article'
    WHEN lower(type) = 'preprint' THEN 'preprint'
    WHEN lower(type) IN ('conference-paper', 'proceedings-article') THEN 'proceedings'
    WHEN lower(type) = 'book' THEN 'book'
    WHEN lower(type) = 'book-chapter' THEN 'chapter'
    WHEN lower(type) = 'dissertation' THEN 'thesis'
    WHEN type IS NOT NULL THEN 'other'
END,
peer_reviewed = CASE
    WHEN lower(doi) LIKE '10.48550/arxiv.%' OR lower(type) = 'preprint' THEN 'false'
    WHEN lower(type) IN ('article', 'review', 'conference-paper', 'proceedings-article') THEN 'true'
    ELSE 'unknown'
END
WHERE source_type IS NULL AND NOT ('source_type' = ANY(manual_fields))"""


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction (a queued ALTER blocks every new reader of papers).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("papers", sa.Column("source_type", sa.Text(), nullable=True))
    op.add_column("papers", sa.Column("peer_reviewed", sa.Text(), server_default=sa.text("'unknown'"), nullable=False))
    op.add_column("papers", sa.Column("published_doi", sa.Text(), nullable=True))
    op.create_check_constraint("ck_papers_source_type", "papers", f"source_type IN ({SOURCE_TYPES})")
    op.create_check_constraint("ck_papers_peer_reviewed", "papers", "peer_reviewed IN ('true','false','unknown')")
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_constraint("ck_papers_peer_reviewed", "papers")
    op.drop_constraint("ck_papers_source_type", "papers")
    op.drop_column("papers", "published_doi")
    op.drop_column("papers", "peer_reviewed")
    op.drop_column("papers", "source_type")
