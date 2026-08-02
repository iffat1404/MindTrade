"""Unit tests for portfolio_engine.py: FIFO lot consumption (the part most
likely to be subtly wrong -- realized P&L on partial closes, direction
flips long<->short) plus get_portfolio/get_sector_exposure.
"""

import uuid
from decimal import Decimal

import pytest

from app.core.security import get_password_hash
from app.models.orm import Account, Position
from app.services import portfolio_engine as pe

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_account(db, *, cash="1000000.00") -> Account:
    account = Account(
        username=f"pytest_pf_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status="APPROVED",
        cash_balance=Decimal(cash),
        starting_capital=Decimal(cash),
    )
    db.add(account)
    await db.flush()
    return account


async def _position(db, account_id, ticker) -> Position:
    from sqlalchemy import select

    return await db.scalar(
        select(Position).where(
            Position.account_id == account_id, Position.ticker == ticker,
            Position.is_backtest.is_(False), Position.is_intraday.is_(False),
        )
    )


# ---------------------------------------------------------------------------
# apply_fill_to_position: opening / extending
# ---------------------------------------------------------------------------
async def test_open_long_position(db):
    account = await _make_account(db)
    realized = await pe.apply_fill_to_position(
        db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("200.00")
    )
    assert realized == Decimal("0")
    pos = await _position(db, account.id, "AAPL")
    assert pos.signed_qty == 10
    assert pos.avg_cost == Decimal("200.00")
    assert pos.realized_pnl == Decimal("0")


async def test_extend_long_position_averages_cost(db):
    account = await _make_account(db)
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("100.00"))
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("200.00"))
    pos = await _position(db, account.id, "AAPL")
    assert pos.signed_qty == 20
    assert pos.avg_cost == Decimal("150.00")  # (10*100 + 10*200) / 20


async def test_open_short_position(db):
    account = await _make_account(db)
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="TSLA", order_side="SELL", fill_qty=15, fill_price=Decimal("250.00"))
    pos = await _position(db, account.id, "TSLA")
    assert pos.signed_qty == -15
    assert pos.avg_cost == Decimal("250.00")


# ---------------------------------------------------------------------------
# apply_fill_to_position: FIFO partial close realizes correct P&L
# ---------------------------------------------------------------------------
async def test_fifo_partial_close_uses_oldest_lot_first(db):
    account = await _make_account(db)
    # Two lots at very different prices -- FIFO vs weighted-average would
    # disagree on realized P&L for a partial close, so this is the test
    # that actually proves it's FIFO and not average-cost.
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("100.00"))
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("200.00"))

    # Sell 10 at 150: FIFO closes the $100 lot -> realized = (150-100)*10 = 500
    # Weighted-average would have said (150-150)*10 = 0 -- very different.
    realized = await pe.apply_fill_to_position(
        db, account_id=account.id, ticker="AAPL", order_side="SELL", fill_qty=10, fill_price=Decimal("150.00")
    )
    assert realized == Decimal("500.00")

    pos = await _position(db, account.id, "AAPL")
    assert pos.signed_qty == 10  # the $200 lot remains
    assert pos.avg_cost == Decimal("200.00")
    assert pos.realized_pnl == Decimal("500.00")

    # Selling the remaining 10 at 150 now realizes against the $200 lot:
    # (150-200)*10 = -500
    realized2 = await pe.apply_fill_to_position(
        db, account_id=account.id, ticker="AAPL", order_side="SELL", fill_qty=10, fill_price=Decimal("150.00")
    )
    assert realized2 == Decimal("-500.00")
    pos2 = await _position(db, account.id, "AAPL")
    assert pos2.signed_qty == 0
    assert pos2.realized_pnl == Decimal("0.00")  # 500 - 500


async def test_short_close_realized_pnl_direction(db):
    account = await _make_account(db)
    # Short 10 @ 250 (opened a short), then buy back 10 @ 230 (cheaper ->
    # profit on the short)
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="TSLA", order_side="SELL", fill_qty=10, fill_price=Decimal("250.00"))
    realized = await pe.apply_fill_to_position(
        db, account_id=account.id, ticker="TSLA", order_side="BUY", fill_qty=10, fill_price=Decimal("230.00")
    )
    assert realized == Decimal("200.00")  # (250-230)*10
    pos = await _position(db, account.id, "TSLA")
    assert pos.signed_qty == 0


async def test_overselling_long_flips_to_short(db):
    account = await _make_account(db)
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="WMT", order_side="BUY", fill_qty=10, fill_price=Decimal("70.00"))
    # Sell 15 while only holding 10: closes the 10 long lot, opens a new
    # 5-share short lot
    realized = await pe.apply_fill_to_position(
        db, account_id=account.id, ticker="WMT", order_side="SELL", fill_qty=15, fill_price=Decimal("75.00")
    )
    assert realized == Decimal("50.00")  # (75-70)*10, only the closed 10 realize
    pos = await _position(db, account.id, "WMT")
    assert pos.signed_qty == -5
    assert pos.avg_cost == Decimal("75.00")  # the new short lot's price


# ---------------------------------------------------------------------------
# get_portfolio: unrealized P&L sign correctness (long vs short)
# ---------------------------------------------------------------------------
async def test_portfolio_unrealized_pnl_long_and_short(db):
    account = await _make_account(db)
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("100.00"))
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="TSLA", order_side="SELL", fill_qty=10, fill_price=Decimal("250.00"))

    portfolio = await pe.get_portfolio(db, account)
    by_ticker = {p.ticker: p for p in portfolio.positions}

    aapl_price = by_ticker["AAPL"].current_price
    expected_aapl_unrealized = (aapl_price - Decimal("100.00")) * 10
    assert by_ticker["AAPL"].unrealized_pnl == expected_aapl_unrealized

    tsla_price = by_ticker["TSLA"].current_price
    expected_tsla_unrealized = (tsla_price - Decimal("250.00")) * -10
    assert by_ticker["TSLA"].unrealized_pnl == expected_tsla_unrealized

    assert portfolio.net_worth == account.cash_balance + portfolio.market_value_total


async def test_portfolio_cagr_is_populated_once_feed_has_history(db):
    account = await _make_account(db)
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=1, fill_price=Decimal("100.00"))
    portfolio = await pe.get_portfolio(db, account)
    # No feed_state row yet -> falls back to the latest loaded minute bar,
    # which is well after SIMULATION_START, so this should be populated.
    assert portfolio.cagr is not None


# ---------------------------------------------------------------------------
# get_sector_exposure
# ---------------------------------------------------------------------------
async def test_sector_exposure_groups_and_sums_correctly(db):
    account = await _make_account(db)
    # AAPL and MSFT are both "Information Technology"
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10, fill_price=Decimal("100.00"))
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="MSFT", order_side="BUY", fill_qty=5, fill_price=Decimal("400.00"))
    await pe.apply_fill_to_position(db, account_id=account.id, ticker="WMT", order_side="BUY", fill_qty=10, fill_price=Decimal("70.00"))

    exposure = await pe.get_sector_exposure(db, account)
    by_sector = {e["sector"]: e for e in exposure}

    assert "Information Technology" in by_sector
    assert "Consumer Staples" in by_sector

    portfolio = await pe.get_portfolio(db, account)
    it_positions = [p for p in portfolio.positions if p.sector == "Information Technology"]
    expected_it_value = sum(p.market_value for p in it_positions)
    assert by_sector["Information Technology"]["market_value"] == expected_it_value
