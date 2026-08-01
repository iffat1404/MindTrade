"""initial_schema_15_tables

Revision ID: e06279dbbf6e
Revises:
Create Date: 2026-08-02 03:55:19.806004

Hand-authored rather than produced by `alembic revision --autogenerate`:
autogenerate diffs target metadata against a live database, and no Postgres
instance was reachable in the environment this migration was written in.
Since this is the very first migration (there is no prior schema to diff
against), it instead creates every table straight from
`app.models.orm.Base.metadata`, which is byte-for-byte what
MASTER_BUILD_PLAN Part 3 specifies (verified separately: compiled DDL for
every table was diffed against the plan's SQL by hand).

From Sprint 1 onward, once a real dev database is available, go back to
`alembic revision --autogenerate` for incremental changes -- don't keep
hand-writing migrations.
"""
from typing import Sequence, Union

from alembic import op

from app.models.orm import Base

# revision identifiers, used by Alembic.
revision: str = "e06279dbbf6e"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# One partition per ticker, per MASTER_BUILD_PLAN Part 3's
# `PARTITION BY LIST (ticker)` design for price_history_minute.
TICKER_PARTITIONS = ["AAPL", "GOOG", "IBM", "MSFT", "TSLA", "UL", "WMT"]


def upgrade() -> None:
    bind = op.get_bind()

    # gen_random_uuid() (used as the server_default for every UUID primary
    # key) is provided by pgcrypto.
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    Base.metadata.create_all(bind=bind, checkfirst=False)

    for ticker in TICKER_PARTITIONS:
        op.execute(
            f"CREATE TABLE price_history_minute_{ticker.lower()} "
            f"PARTITION OF price_history_minute FOR VALUES IN ('{ticker}')"
        )


def downgrade() -> None:
    bind = op.get_bind()

    for ticker in TICKER_PARTITIONS:
        op.execute(f"DROP TABLE IF EXISTS price_history_minute_{ticker.lower()}")

    Base.metadata.drop_all(bind=bind)
