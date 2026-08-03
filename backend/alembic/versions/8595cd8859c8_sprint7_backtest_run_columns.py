"""sprint7_backtest_run_columns

Revision ID: 8595cd8859c8
Revises: 9652adf9f63d
Create Date: 2026-08-03 18:02:32.174590

Adds backtest_runs.sharpe_ratio + equity_curve (FRONTEND_DESIGN_GUIDE's
Paper Trading page wants an equity curve chart and an optional Sharpe
ratio, and GET /api/backtest/{id}/results needs somewhere to read the
curve back from without recomputing it), and orders.backtest_run_id (so
GET /api/backtest/{id}/trades can query directly instead of
reconstructing "which orders belong to this run" from is_backtest +
ticker + a time window).

Autogenerate again flagged the 7 price_history_minute_* partition
tables as "removed" -- expected and harmless (raw SQL, not
SQLAlchemy-mapped, see services/data/loaders.py). Those drop_table/
create_table calls are stripped from this migration.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8595cd8859c8'
down_revision: Union[str, None] = '9652adf9f63d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('backtest_runs', sa.Column('sharpe_ratio', sa.Numeric(precision=10, scale=4), nullable=True))
    op.add_column('backtest_runs', sa.Column('equity_curve', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('orders', sa.Column('backtest_run_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_orders_backtest_run_id', 'orders', 'backtest_runs', ['backtest_run_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_orders_backtest_run_id', 'orders', type_='foreignkey')
    op.drop_column('orders', 'backtest_run_id')
    op.drop_column('backtest_runs', 'equity_curve')
    op.drop_column('backtest_runs', 'sharpe_ratio')
