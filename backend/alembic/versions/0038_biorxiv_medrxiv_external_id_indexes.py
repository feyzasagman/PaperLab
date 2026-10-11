"""external_refs partial UNIQUE indexes for the biorxiv and medrxiv external_ids keys (M32 batch 6
phase B), same shape as every prior new-key index (0025, 0027, 0029, 0033, 0035->0036) -- an unindexed
key forces a sequential scan on harvest_ingest_candidate's own find-or-create lookup (D201, D202, D204,
D206). UNIQUE from the start (not non-unique-then-upgraded, unlike 0035->0036) -- Batch 6A's own final
review already established that upgrading later is itself a gap worth skipping. harvest_ingest_candidate
assumes at most one row per source-scoped id; a duplicate would make every future harvest raise
MultipleResultsFound and fail permanently, so this is safe and correct from day one rather than a later
fix.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-10
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY external_refs_external_ids_biorxiv ON external_refs "
            "((external_ids->>'biorxiv')) WHERE external_ids->>'biorxiv' IS NOT NULL"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY external_refs_external_ids_medrxiv ON external_refs "
            "((external_ids->>'medrxiv')) WHERE external_ids->>'medrxiv' IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS external_refs_external_ids_medrxiv")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS external_refs_external_ids_biorxiv")
