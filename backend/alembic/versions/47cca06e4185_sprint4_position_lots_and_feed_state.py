"""sprint4_position_lots_and_feed_state

Revision ID: 47cca06e4185
Revises: 6d4d59aea235
Create Date: 2026-08-02 17:23:55.716420

Adds two Sprint 4 tables not in the original 15-table schema:
- position_lots: a genuine FIFO queue (Sprint 4's spec explicitly asks for
  "FIFO lots" for realized P&L on partial closes; positions.avg_cost is a
  single blended value that can't represent that).
- feed_state: durable state for the feed simulator's replay position
  (current tick, running/paused, speed), so GET /api/admin/feed/status and
  POST .../reset survive an app restart.

Autogenerate also flagged the 7 price_history_minute_* partitions as
"removed" -- they're raw SQL (created via op.execute in the initial
migration, not SQLAlchemy-mapped tables), so autogenerate has no way to
know about them and assumes they shouldn't exist. Those spurious
drop/recreate calls are stripped out below; this migration only touches
the two new tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "47cca06e4185"
down_revision: Union[str, None] = "6d4d59aea235"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_state",
        sa.Column("id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("current_tick_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_running", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("speed_multiplier", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint("id", name="ck_feed_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "position_lots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("is_backtest", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_intraday", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("side", sa.String(length=5), nullable=False),
        sa.Column("qty_remaining", sa.Integer(), nullable=False),
        sa.Column("cost_price", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("fill_id", sa.UUID(), nullable=True),
        sa.CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_position_lots_side"),
        sa.CheckConstraint("qty_remaining >= 0", name="ck_position_lots_qty_remaining_non_negative"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["fill_id"], ["fills.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_position_lots_account_ticker",
        "position_lots",
        ["account_id", "ticker", "is_backtest", "is_intraday", "opened_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_position_lots_account_ticker", table_name="position_lots")
    op.drop_table("position_lots")
    op.drop_table("feed_state")
