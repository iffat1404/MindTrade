import operator
import re
import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import BacktestRun, Fill, Order, PriceHistoryDaily, Strategy
from app.services import indicators
from app.services.portfolio_engine import apply_fill_to_position

# Task 7.1 (MASTER_BUILD_PLAN Sprint 7): entry/exit rules are free-text
# strings like "RSI<30" -- the only concrete grammar the docs specify.
# Supports the 4 indicators indicators.py already computes, compared
# against a numeric threshold with <, >, <=, >=.
_RULE_PATTERN = re.compile(r"^\s*(RSI|SMA|EMA|MACD)\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_OPS = {"<": operator.lt, ">": operator.gt, "<=": operator.le, ">=": operator.ge}


def _parse_rule(rule: str) -> tuple[str, str, float]:
    match = _RULE_PATTERN.match(rule)
    if not match:
        raise ValueError(f"Unsupported rule syntax: {rule!r} (expected e.g. 'RSI<30')")
    indicator_name, op_symbol, threshold = match.groups()
    return indicator_name.upper(), op_symbol, float(threshold)


@dataclass
class BacktestTrade:
    side: str
    date: date_type
    price: Decimal
    qty: int
    realized_pnl: Optional[Decimal]


@dataclass
class EquityPoint:
    date: date_type
    equity: float


@dataclass
class BacktestResult:
    backtest_run_id: uuid.UUID
    total_return: Optional[Decimal]
    max_drawdown: Optional[Decimal]
    win_rate: Optional[Decimal]
    benchmark_return: Optional[Decimal]
    sharpe_ratio: Optional[Decimal]
    equity_curve: list[EquityPoint] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)


async def _record_backtest_fill(
    db: AsyncSession, *, account_id: uuid.UUID, ticker: str, side: str, qty: int, price: Decimal,
    as_of: date_type, backtest_run_id: uuid.UUID,
) -> Decimal:
    """Creates a real Order+Fill (is_backtest=True, linked to this run) and
    applies it via the same FIFO-lot accounting live fills use -- Position/
    PositionLot rows are is_backtest-scoped, so this never touches (or
    shows up in) the account's live portfolio (US-7.3). Cash is tracked
    purely in-memory by the caller, not via CashLedger/Account.cash_balance,
    since a backtest's capital is independent of the account's real balance.
    """
    fill_time = datetime.combine(as_of, time(9, 30), tzinfo=timezone.utc)
    order = Order(
        account_id=account_id, ticker=ticker, side=side, order_type="MARKET", product_type="CNC",
        qty=qty, remaining_qty=0, status="FILLED", is_backtest=True, backtest_run_id=backtest_run_id,
        created_at=fill_time,
    )
    db.add(order)
    await db.flush()

    fill = Fill(
        order_id=order.id, fill_price=price, fill_qty=qty, fees=Decimal("0"), reason="BACKTEST",
        is_backtest=True, timestamp=fill_time,
    )
    db.add(fill)
    await db.flush()

    realized = await apply_fill_to_position(
        db, account_id=account_id, ticker=ticker, order_side=side, fill_qty=qty, fill_price=price,
        is_backtest=True, fill_id=fill.id,
    )
    fill.realized_pnl = realized
    return realized


async def run_backtest(
    db: AsyncSession,
    *,
    account_id: uuid.UUID,
    ticker: str,
    entry_rule: str,
    exit_rule: str,
    position_size: int,
    start_date: date_type,
    end_date: date_type,
) -> BacktestResult:
    """Simulates `entry_rule`/`exit_rule` against `ticker`'s daily bars from
    `start_date` to `end_date`, one fixed-size position at a time (long
    only). Persists a Strategy + BacktestRun plus real is_backtest=True
    Order/Fill/Position rows for every simulated trade, so the results are
    genuinely auditable (Task 7.1's "flags is_backtest=true on all
    generated orders/fills"), not just an in-memory number.
    """
    entry_indicator, entry_op, entry_threshold = _parse_rule(entry_rule)
    exit_indicator, exit_op, exit_threshold = _parse_rule(exit_rule)

    bars = (
        await db.scalars(
            select(PriceHistoryDaily)
            .where(PriceHistoryDaily.ticker == ticker, PriceHistoryDaily.date <= end_date)
            .order_by(PriceHistoryDaily.date.asc())
        )
    ).all()
    if not bars:
        raise ValueError(f"No price history for {ticker}")

    closes = pd.Series([float(b.close) for b in bars], index=[b.date for b in bars])
    series = {
        "RSI": indicators.rsi(closes),
        "SMA": indicators.sma(closes),
        "EMA": indicators.ema(closes),
        "MACD": indicators.macd(closes)["macd"],
    }

    strategy = Strategy(
        account_id=account_id, ticker=ticker, entry_rule=entry_rule, exit_rule=exit_rule,
        position_size=position_size,
    )
    db.add(strategy)
    await db.flush()
    run = BacktestRun(strategy_id=strategy.id)
    db.add(run)
    await db.flush()

    window_dates = [d for d in closes.index if d >= start_date]
    if not window_dates:
        raise ValueError(f"No price history for {ticker} within {start_date}..{end_date}")

    starting_capital = settings.STARTING_CAPITAL
    cash = starting_capital
    shares = 0
    avg_cost = Decimal("0")
    starting_price: Optional[Decimal] = None
    equity_curve: list[EquityPoint] = []
    trades: list[BacktestTrade] = []
    wins = 0
    closed_trades = 0

    for d in window_dates:
        i = closes.index.get_loc(d)
        price = Decimal(str(closes.iloc[i]))
        if starting_price is None:
            starting_price = price

        entry_value = series[entry_indicator].iloc[i]
        exit_value = series[exit_indicator].iloc[i]

        if shares == 0 and not pd.isna(entry_value) and _OPS[entry_op](entry_value, entry_threshold):
            shares = position_size
            avg_cost = price
            cash -= price * position_size
            await _record_backtest_fill(
                db, account_id=account_id, ticker=ticker, side="BUY", qty=position_size, price=price,
                as_of=d, backtest_run_id=run.id,
            )
            trades.append(BacktestTrade(side="BUY", date=d, price=price, qty=position_size, realized_pnl=None))

        elif shares > 0 and not pd.isna(exit_value) and _OPS[exit_op](exit_value, exit_threshold):
            realized = (price - avg_cost) * shares
            cash += price * shares
            await _record_backtest_fill(
                db, account_id=account_id, ticker=ticker, side="SELL", qty=shares, price=price,
                as_of=d, backtest_run_id=run.id,
            )
            trades.append(BacktestTrade(side="SELL", date=d, price=price, qty=shares, realized_pnl=realized))
            closed_trades += 1
            wins += 1 if realized > 0 else 0
            shares = 0
            avg_cost = Decimal("0")

        equity_curve.append(EquityPoint(date=d, equity=float(cash + shares * price)))

    # Force-close any still-open position at the last bar so total_return
    # reflects a clean mark-to-market rather than stranded inventory.
    if shares > 0:
        last_date = window_dates[-1]
        last_price = Decimal(str(closes.iloc[-1]))
        realized = (last_price - avg_cost) * shares
        cash += last_price * shares
        await _record_backtest_fill(
            db, account_id=account_id, ticker=ticker, side="SELL", qty=shares, price=last_price,
            as_of=last_date, backtest_run_id=run.id,
        )
        trades.append(BacktestTrade(side="SELL", date=last_date, price=last_price, qty=shares, realized_pnl=realized))
        closed_trades += 1
        wins += 1 if realized > 0 else 0
        equity_curve[-1] = EquityPoint(date=last_date, equity=float(cash))

    final_equity = equity_curve[-1].equity if equity_curve else float(starting_capital)
    total_return = Decimal(str(round((final_equity - float(starting_capital)) / float(starting_capital), 6)))

    peak = float(starting_capital)
    max_dd = 0.0
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - point.equity) / peak)
    max_drawdown = Decimal(str(round(max_dd, 6)))

    win_rate = Decimal(str(round(wins / closed_trades, 4))) if closed_trades else None

    last_close = closes.iloc[-1]
    benchmark_return = (
        Decimal(str(round((last_close - float(starting_price)) / float(starting_price), 6)))
        if starting_price
        else None
    )

    daily_returns = pd.Series([p.equity for p in equity_curve]).pct_change().dropna()
    sharpe_ratio = None
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe_ratio = Decimal(str(round((daily_returns.mean() / daily_returns.std()) * (252**0.5), 4)))

    run.total_return = total_return
    run.max_drawdown = max_drawdown
    run.win_rate = win_rate
    run.benchmark_return = benchmark_return
    run.sharpe_ratio = sharpe_ratio
    run.equity_curve = [{"date": p.date.isoformat(), "equity": p.equity} for p in equity_curve]

    return BacktestResult(
        backtest_run_id=run.id,
        total_return=total_return,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        benchmark_return=benchmark_return,
        sharpe_ratio=sharpe_ratio,
        equity_curve=equity_curve,
        trades=trades,
    )
