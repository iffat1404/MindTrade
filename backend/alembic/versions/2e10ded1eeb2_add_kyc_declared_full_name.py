"""add_kyc_declared_full_name

Revision ID: 2e10ded1eeb2
Revises: e06279dbbf6e
Create Date: 2026-08-02 06:30:27.446782

Adds KYCSubmission.declared_full_name (Sprint 2): the name the trader
declares at submission time, matched against the GenAI-extracted name by
kyc_engine's name_match check. Added nullable first and backfilled from
extracted_full_name (the best available proxy for any rows seeded before
this column existed) before enforcing NOT NULL, so this is safe to run
against an already-seeded dev database, not just a fresh one.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e10ded1eeb2"
down_revision: Union[str, None] = "e06279dbbf6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("kyc_submissions", sa.Column("declared_full_name", sa.Text(), nullable=True))
    op.execute("UPDATE kyc_submissions SET declared_full_name = extracted_full_name WHERE declared_full_name IS NULL")
    op.alter_column("kyc_submissions", "declared_full_name", nullable=False)


def downgrade() -> None:
    op.drop_column("kyc_submissions", "declared_full_name")
