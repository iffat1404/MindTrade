"""sprint6_fill_realized_pnl

Revision ID: 9652adf9f63d
Revises: 47cca06e4185
Create Date: 2026-08-03 17:06:38.420356

Adds fills.realized_pnl (Sprint 6): the FIFO-lot realized P&L a specific
fill generated (0 if it only opened/extended a position). Previously this
value was computed by apply_fill_to_position but only ever passed
transiently into behavioral_guard.record_fill_outcome (a capped-at-20
JSONB blob) or logged -- never persisted anywhere queryable by date.
Session Review needs to reliably compute win/loss outcomes for an
arbitrary past trading day, which the capped rolling buffer can't
guarantee once more than ~20 fills have happened since.

Autogenerate also flagged the 7 price_history_minute_* partition tables
as "removed" -- expected and harmless, they're raw SQL tables (see
services/data/loaders.py), not SQLAlchemy-mapped, so autogenerate always
misdetects them as dropped. Those drop_table/create_table calls are
stripped from this migration; only the fills column change is real.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9652adf9f63d'
down_revision: Union[str, None] = '47cca06e4185'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('fills', sa.Column('realized_pnl', sa.Numeric(precision=15, scale=2), nullable=True))


def downgrade() -> None:
    op.drop_column('fills', 'realized_pnl')
