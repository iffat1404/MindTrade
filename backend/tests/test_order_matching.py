"""Unit tests for order_matching.py: synthetic fills (MARKET, triggered
SL/SL-M, limit-vs-tick), real trader-vs-trader LIMIT_CROSS matching with
price-time priority, partial fills, and the account-level side effects
(cash, margin_used, position updates) each fill should produce.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.security import get_password_hash
from app.models.orm import Account, Order, OrderMatch, Position
from app.services import order_matching as om

pytestmark = pytest.mark.asyncio(loop_scope="session")

TICK_TIME = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _make_account(db, *, cash="1000000.00") -> Account:
    account = Account(
        username=f"pytest_om_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status="APPROVED",
        cash_balance=Decimal(cash),
        starting_capital=Decimal(cash),
    )
    db.add(account)
    await db.flush()
    return account


async def _make_order(db, account, *, side, order_type="MARKET", qty=10, limit_price=None,
                       stop_trigger_price=None, stop_limit_price=None, product_type="CNC",
                       ticker="AAPL", created_at=None) -> Order:
    order = Order(
        account_id=account.id, ticker=ticker, side=side, order_type=order_type,
        product_type=product_type, qty=qty, remaining_qty=qty,
        limit_price=limit_price, stop_trigger_price=stop_trigger_price, stop_limit_price=stop_limit_price,
        status="ROUTED",
    )
    if created_at is not None:
        order.created_at = created_at
    db.add(order)
    await db.flush()
    return order


def _tick(**kw):
    defaults = dict(ticker="AAPL", timestamp=TICK_TIME, open=Decimal("232"), high=Decimal("234"),
                    low=Decimal("230"), close=Decimal("232.98"), volume=1000)
    defaults.update(kw)
    return om.TickBar(**defaults)


async def _refresh(db, order: Order) -> Order:
    await db.refresh(order)
    return order


# ---------------------------------------------------------------------------
# Synthetic fills
# ---------------------------------------------------------------------------
async def test_market_buy_fills_at_synthetic_ask(db):
    account = await _make_account(db)
    order = await _make_order(db, account, side="BUY", order_type="MARKET", qty=5)
    await om.process_tick(db, _tick())
    await _refresh(db, order)

    assert order.status == "FILLED"
    assert order.remaining_qty == 0
    # synthetic ask is close + half the spread -> strictly above close
    match = await db.scalar(select(OrderMatch).where(OrderMatch.order_id == order.id))
    assert match.match_type == "SYNTHETIC_ASK"
    assert match.fill_price > Decimal("232.98")


async def test_market_sell_fills_at_synthetic_bid(db):
    account = await _make_account(db)
    order = await _make_order(db, account, side="SELL", order_type="MARKET", qty=5)
    await om.process_tick(db, _tick())
    await _refresh(db, order)

    match = await db.scalar(select(OrderMatch).where(OrderMatch.order_id == order.id))
    assert match.match_type == "SYNTHETIC_BID"
    assert match.fill_price < Decimal("232.98")


async def test_limit_buy_no_counterparty_fills_when_tick_reaches_price(db):
    account = await _make_account(db)
    # tick low=230, so a buy limit at 231 should fill (market touched 230 <= 231)
    order = await _make_order(db, account, side="BUY", order_type="LIMIT", qty=5, limit_price=Decimal("231.00"))
    await om.process_tick(db, _tick())
    await _refresh(db, order)
    assert order.status == "FILLED"


async def test_limit_buy_no_counterparty_stays_resting_when_tick_misses_price(db):
    account = await _make_account(db)
    # tick low=230, buy limit at 200 never gets touched
    order = await _make_order(db, account, side="BUY", order_type="LIMIT", qty=5, limit_price=Decimal("200.00"))
    await om.process_tick(db, _tick())
    await _refresh(db, order)
    assert order.status == "ROUTED"
    assert order.remaining_qty == 5


async def test_sl_m_not_triggered_stays_resting(db):
    account = await _make_account(db)
    # BUY SL-M trigger 300 -- tick high is only 234, nowhere near
    order = await _make_order(db, account, side="BUY", order_type="SL-M", qty=5, stop_trigger_price=Decimal("300.00"))
    await om.process_tick(db, _tick())
    await _refresh(db, order)
    assert order.status == "ROUTED"


async def test_sl_m_triggered_fills_at_synthetic_price(db):
    account = await _make_account(db)
    # BUY SL-M trigger 233 -- tick high is 234, so it triggers
    order = await _make_order(db, account, side="BUY", order_type="SL-M", qty=5, stop_trigger_price=Decimal("233.00"))
    await om.process_tick(db, _tick())
    await _refresh(db, order)
    assert order.status == "FILLED"
    match = await db.scalar(select(OrderMatch).where(OrderMatch.order_id == order.id))
    assert match.match_type == "SYNTHETIC_ASK"


async def test_sl_triggered_fills_at_stop_limit_price(db):
    account = await _make_account(db)
    # SELL SL: trigger 231 (tick low=230 crosses it), stop_limit 229 -- tick
    # high=234 >= 229, so the limit condition is met too
    order = await _make_order(
        db, account, side="SELL", order_type="SL", qty=5,
        stop_trigger_price=Decimal("231.00"), stop_limit_price=Decimal("229.00"),
    )
    await om.process_tick(db, _tick())
    await _refresh(db, order)
    assert order.status == "FILLED"
    match = await db.scalar(select(OrderMatch).where(OrderMatch.order_id == order.id))
    assert match.fill_price == Decimal("229.00")


# ---------------------------------------------------------------------------
# Real trader-vs-trader LIMIT_CROSS matching
# ---------------------------------------------------------------------------
async def test_limit_cross_matches_at_resting_orders_price(db):
    buyer = await _make_account(db)
    seller = await _make_account(db)
    # seller rests first at 184.50; buyer arrives later willing to pay up to 184.60
    sell_order = await _make_order(
        db, seller, side="SELL", order_type="LIMIT", qty=10, limit_price=Decimal("184.50"),
        created_at=TICK_TIME - timedelta(minutes=5),
    )
    buy_order = await _make_order(
        db, buyer, side="BUY", order_type="LIMIT", qty=10, limit_price=Decimal("184.60"),
        created_at=TICK_TIME - timedelta(minutes=1),
    )
    await om.process_tick(db, _tick(low=Decimal("184.00"), high=Decimal("185.00")))
    await _refresh(db, sell_order)
    await _refresh(db, buy_order)

    assert sell_order.status == "FILLED"
    assert buy_order.status == "FILLED"

    match = await db.scalar(select(OrderMatch).where(OrderMatch.order_id == buy_order.id))
    assert match.match_type == "LIMIT_CROSS"
    # seller was resting first -> match price is the seller's price
    assert match.fill_price == Decimal("184.50")
    assert match.counterparty_account_id == seller.id


async def test_limit_cross_partial_fill_leaves_larger_order_resting(db):
    buyer = await _make_account(db)
    seller = await _make_account(db)
    sell_order = await _make_order(db, seller, side="SELL", order_type="LIMIT", qty=5, limit_price=Decimal("100.00"))
    buy_order = await _make_order(db, buyer, side="BUY", order_type="LIMIT", qty=20, limit_price=Decimal("100.00"))

    await om.process_tick(db, _tick(ticker="AAPL", low=Decimal("99"), high=Decimal("101")))
    await _refresh(db, sell_order)
    await _refresh(db, buy_order)

    assert sell_order.status == "FILLED"
    assert sell_order.remaining_qty == 0
    assert buy_order.status == "ROUTED"
    assert buy_order.remaining_qty == 15


# ---------------------------------------------------------------------------
# Account-level side effects
# ---------------------------------------------------------------------------
async def test_fill_updates_cash_balance_and_position(db):
    account = await _make_account(db, cash="1000000.00")
    order = await _make_order(db, account, side="BUY", order_type="MARKET", qty=10)
    await om.process_tick(db, _tick())
    # NOTE: session.refresh() does NOT autoflush pending changes first
    # (unlike a plain query), so it would silently overwrite the
    # just-applied in-memory cash_balance update with the stale DB value.
    # A fresh query does trigger autoflush, so it sees the pending change.
    account = await db.scalar(select(Account).where(Account.id == account.id))

    match = await db.scalar(select(OrderMatch).where(OrderMatch.order_id == order.id))
    expected_cash = (Decimal("1000000.00") - (match.fill_price * 10) - Decimal("1.00")).quantize(Decimal("0.01"))
    assert account.cash_balance == expected_cash

    position = await db.scalar(
        select(Position).where(Position.account_id == account.id, Position.ticker == "AAPL")
    )
    assert position.signed_qty == 10


async def test_mis_fill_updates_margin_used(db):
    account = await _make_account(db, cash="1000000.00")
    await _make_order(db, account, side="BUY", order_type="MARKET", qty=10, product_type="MIS")
    await om.process_tick(db, _tick())
    account = await db.scalar(select(Account).where(Account.id == account.id))

    assert account.margin_used > 0

    position = await db.scalar(
        select(Position).where(
            Position.account_id == account.id, Position.ticker == "AAPL", Position.is_intraday.is_(True),
        )
    )
    expected_margin = abs(position.signed_qty) * position.avg_cost * Decimal("1.5")
    assert account.margin_used == expected_margin
