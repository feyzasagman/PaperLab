"""paper_sources.biorxiv_enabled, paper_sources.medrxiv_enabled: bioRxiv and medRxiv join the registry
as sources 17 and 18 (M32 batch 6 phase B) -- no key, the real api.biorxiv.org host needs none. Both
reuse the harvest_cursors table Batch 6A already created (its PK is a bare `source` string, generic to
any periodic-harvest source) -- no new table here, just two settings columns.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-10
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE paper_sources ADD COLUMN biorxiv_enabled boolean NOT NULL DEFAULT true",
    "ALTER TABLE paper_sources ADD COLUMN medrxiv_enabled boolean NOT NULL DEFAULT true",
]

DOWNGRADE = [
    "ALTER TABLE paper_sources DROP COLUMN medrxiv_enabled",
    "ALTER TABLE paper_sources DROP COLUMN biorxiv_enabled",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
