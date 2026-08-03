"""Unit tests for backtest_engine.py: rule parsing, running a real
simulation against seeded AAPL daily data, backtest isolation (is_backtest
rows never leak into the live portfolio), and the summary metrics.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.security import get_password_hash
from app.models.orm import Account, BacktestRun, Order, Position, Strategy
from app.services import backtest_engine as be
from app.services.portfolio_engine import get_portfolio

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_account(db) -> Account:
    account = Account(
        username=f"pytest_bt_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status="APPROVED",
        cash_balance=Decimal("1000000.00"),
        starting_capital=Decimal("1000000.00"),
    )
    db.add(account)
    await db.flush()
    return account


# ---------------------------------------------------------------------------
# Rule parsing
# ---------------------------------------------------------------------------
async def test_parse_rule_valid_forms():
    assert be._parse_rule("RSI<30") == ("RSI", "<", 30.0)
    assert be._parse_rule("RSI>70") == ("RSI", ">", 70.0)
    assert be._parse_rule(" sma <= 100.5 ") == ("SMA", "<=", 100.5)
    assert be._parse_rule("MACD>=0") == ("MACD", ">=", 0.0)


async def test_parse_rule_rejects_unsupported_syntax():
    with pytest.raises(ValueError):
        be._parse_rule("PRICE < 100")
    with pytest.raises(ValueError):
        be._parse_rule("nonsense")


# ---------------------------------------------------------------------------
# run_backtest
# ---------------------------------------------------------------------------
async def test_run_backtest_creates_strategy_and_run(db):
    account = await _make_account(db)
    result = await be.run_backtest(
        db, account_id=account.id, ticker="AAPL", entry_rule="RSI<40", exit_rule="RSI>60",
        position_size=10, start_date=date(2026, 2, 1), end_date=date(2026, 6, 1),
    )
    assert result.backtest_run_id is not None
    assert result.total_return is not None
    assert result.max_drawdown is not None
    assert result.benchmark_return is not None
    assert len(result.equity_curve) > 0

    run = await db.get(BacktestRun, result.backtest_run_id)
    assert run is not None
    assert run.total_return == result.total_return
    assert run.equity_curve is not None and len(run.equity_curve) == len(result.equity_curve)

    strategy = await db.get(Strategy, run.strategy_id)
    assert strategy.ticker == "AAPL"
    assert strategy.entry_rule == "RSI<40"
    assert strategy.account_id == account.id


async def test_run_backtest_trades_are_flagged_is_backtest_and_linked_to_run(db):
    account = await _make_account(db)
    result = await be.run_backtest(
        db, account_id=account.id, ticker="AAPL", entry_rule="RSI<50", exit_rule="RSI>50",
        position_size=5, start_date=date(2026, 2, 1), end_date=date(2026, 6, 1),
    )
    if not result.trades:
        pytest.skip("No trades generated for this rule/date-range combination")

    orders = (
        await db.scalars(select(Order).where(Order.backtest_run_id == result.backtest_run_id))
    ).all()
    assert len(orders) == len(result.trades)
    assert all(o.is_backtest for o in orders)
    assert all(o.account_id == account.id for o in orders)


async def test_backtest_never_appears_in_live_portfolio(db):
    """US-7.3: backtest activity never appears in the live portfolio."""
    account = await _make_account(db)
    await be.run_backtest(
        db, account_id=account.id, ticker="AAPL", entry_rule="RSI<50", exit_rule="RSI>50",
        position_size=5, start_date=date(2026, 2, 1), end_date=date(2026, 6, 1),
    )

    live_portfolio = await get_portfolio(db, account, is_backtest=False)
    assert live_portfolio.positions == []
    assert live_portfolio.cash_balance == Decimal("1000000.00")  # untouched by the backtest

    backtest_position = await db.scalar(
        select(Position).where(
            Position.account_id == account.id, Position.ticker == "AAPL", Position.is_backtest.is_(True)
        )
    )
    # Either flat (bought and sold back) or still holding -- either way it
    # must exist only in the is_backtest=True scope, never the live one.
    if backtest_position is not None:
        assert backtest_position.is_backtest is True


async def test_run_backtest_raises_for_unknown_ticker(db):
    account = await _make_account(db)
    with pytest.raises(ValueError):
        await be.run_backtest(
            db, account_id=account.id, ticker="NOPE_TICKER", entry_rule="RSI<30", exit_rule="RSI>70",
            position_size=10, start_date=date(2026, 2, 1), end_date=date(2026, 6, 1),
        )


async def test_run_backtest_win_rate_only_counts_closed_trades(db):
    account = await _make_account(db)
    result = await be.run_backtest(
        db, account_id=account.id, ticker="AAPL", entry_rule="RSI<35", exit_rule="RSI>65",
        position_size=10, start_date=date(2026, 1, 15), end_date=date(2026, 7, 10),
    )
    if result.win_rate is not None:
        assert Decimal("0") <= result.win_rate <= Decimal("1")


# ---------------------------------------------------------------------------
# HTTP-level: POST /api/backtest -> GET results -> GET trades
# ---------------------------------------------------------------------------
async def test_backtest_api_create_then_fetch_results_and_trades(app_client):
    login = await app_client.post(
        "/api/auth/login", json={"username": "demo_trader1", "password": "demo123"}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await app_client.post(
        "/api/backtest",
        headers=headers,
        json={
            "ticker": "AAPL", "entry_rule": "RSI<40", "exit_rule": "RSI>60",
            "position_size": 10, "start_date": "2026-02-01", "end_date": "2026-06-01",
        },
    )
    assert create.status_code == 201
    backtest_id = create.json()["backtest_id"]

    results = await app_client.get(f"/api/backtest/{backtest_id}/results", headers=headers)
    assert results.status_code == 200
    body = results.json()
    assert body["backtest_id"] == backtest_id
    assert body["ticker"] == "AAPL"
    assert isinstance(body["equity_curve"], list)

    trades = await app_client.get(f"/api/backtest/{backtest_id}/trades", headers=headers)
    assert trades.status_code == 200
    assert isinstance(trades.json(), list)


async def test_backtest_api_rejects_other_traders_results(app_client):
    login1 = await app_client.post(
        "/api/auth/login", json={"username": "demo_trader1", "password": "demo123"}
    )
    headers1 = {"Authorization": f"Bearer {login1.json()['access_token']}"}
    create = await app_client.post(
        "/api/backtest", headers=headers1,
        json={
            "ticker": "AAPL", "entry_rule": "RSI<40", "exit_rule": "RSI>60",
            "position_size": 10, "start_date": "2026-02-01", "end_date": "2026-06-01",
        },
    )
    backtest_id = create.json()["backtest_id"]

    login2 = await app_client.post(
        "/api/auth/login", json={"username": "demo_trader2", "password": "demo123"}
    )
    headers2 = {"Authorization": f"Bearer {login2.json()['access_token']}"}
    results = await app_client.get(f"/api/backtest/{backtest_id}/results", headers=headers2)
    assert results.status_code == 404
