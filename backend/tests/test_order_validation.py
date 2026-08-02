"""Task 3.6: unit tests for the order-engine checks that need a real DB
session (concentration, buying power, rate limit, wash trade, MIS margin),
plus full-chain validate_order() integration tests. Pure (no-DB) checks
live in test_order_checks_pure.py -- kept separate so pytest-asyncio's
loop_scope marker below (needed for these async DB tests) doesn't also
land on synchronous tests, which just produces warning noise.

Using a real AsyncSession against the live test database (see conftest.py)
is what lets INSUFFICIENT_BUYING_POWER and INSUFFICIENT_MARGIN actually be
exercised: both are structurally unreachable through the full HTTP chain
under the current placeholder constants (MAX_NOTIONAL_PER_ORDER=500k always
rejects first for any account with a realistic starting cash balance), but
are real code paths that need real coverage, so they're called directly
here with a purpose-built low-cash account instead of fighting
check-ordering.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.security import get_password_hash
from app.models.orm import Account, Order, Position
from app.services.order_engine import (
    check_buying_power,
    check_concentration_limit,
    check_mis_margin,
    check_rate_limit,
    check_wash_trade,
    get_last_price,
    get_latest_market_time,
    validate_order,
)

from .test_order_checks_pure import _order

# Belt-and-suspenders alongside pytest.ini's asyncio_default_fixture_loop_scope:
# that setting affects async *fixtures* (app_client, db), but pytest-asyncio
# 0.24 has no equivalent ini default for async *test* functions -- without
# this, auto-detected async tests get their own per-test loop that conflicts
# with the session-scoped engine connections opened during app_client's
# lifespan ("attached to a different loop" RuntimeError).
pytestmark = pytest.mark.asyncio(loop_scope="session")

MARKET_OPEN = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)  # well within 09:30-16:00 UTC


async def _make_account(db, *, cash="1000000.00", kyc_status="APPROVED") -> Account:
    account = Account(
        username=f"pytest_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status=kyc_status,
        cash_balance=Decimal(cash),
        starting_capital=Decimal(cash),
    )
    db.add(account)
    await db.flush()
    return account


# ---------------------------------------------------------------------------
# get_last_price / get_latest_market_time
# ---------------------------------------------------------------------------
async def test_get_last_price_known_ticker(db):
    price = await get_last_price(db, "AAPL")
    assert price is not None and price > 0


async def test_get_last_price_unknown_ticker(db):
    assert await get_last_price(db, "ZZZZ") is None


async def test_get_latest_market_time(db):
    ts = await get_latest_market_time(db)
    assert ts is not None
    assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# check_concentration_limit
# ---------------------------------------------------------------------------
async def test_concentration_limit_small_order_passes(db):
    account = await _make_account(db)
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    result = await check_concentration_limit(order, Decimal("233.00"), account, db)
    assert result.passed


async def test_concentration_limit_large_order_fails(db):
    account = await _make_account(db)  # 1M cash, no positions
    # ~300k of AAPL against ~1M net worth is >30%
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1300)
    result = await check_concentration_limit(order, Decimal("233.00"), account, db)
    assert not result.passed
    assert result.reason_code == "CONCENTRATION_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# check_buying_power
# ---------------------------------------------------------------------------
async def test_buying_power_mis_always_passes(db):
    # MIS capital requirements are check_mis_margin's job, not this check's
    account = await _make_account(db, cash="1.00")
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1000, product_type="MIS")
    result = await check_buying_power(order, Decimal("233.00"), account, db)
    assert result.passed


async def test_buying_power_cnc_buy_sufficient_cash_passes(db):
    account = await _make_account(db, cash="1000000.00")
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    result = await check_buying_power(order, Decimal("233.00"), account, db)
    assert result.passed


async def test_buying_power_cnc_buy_insufficient_cash_fails(db):
    account = await _make_account(db, cash="10.00")
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    result = await check_buying_power(order, Decimal("233.00"), account, db)
    assert not result.passed
    assert result.reason_code == "INSUFFICIENT_BUYING_POWER"


async def test_buying_power_cnc_sell_with_holdings_passes(db):
    account = await _make_account(db)
    db.add(Position(account_id=account.id, ticker="AAPL", signed_qty=10, avg_cost=Decimal("200.00")))
    await db.flush()
    order = _order(ticker="AAPL", side="SELL", type="MARKET", qty=5, product_type="CNC")
    result = await check_buying_power(order, Decimal("233.00"), account, db)
    assert result.passed


async def test_buying_power_cnc_sell_without_holdings_fails(db):
    account = await _make_account(db)
    order = _order(ticker="AAPL", side="SELL", type="MARKET", qty=5, product_type="CNC")
    result = await check_buying_power(order, Decimal("233.00"), account, db)
    assert not result.passed
    assert result.reason_code == "INSUFFICIENT_HOLDINGS"


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------
async def test_rate_limit_under_cap_passes(db):
    account = await _make_account(db)
    for _ in range(5):
        db.add(
            Order(
                account_id=account.id, ticker="AAPL", side="BUY", order_type="MARKET",
                qty=1, remaining_qty=1, status="REJECTED",
            )
        )
    await db.flush()
    result = await check_rate_limit(account, db)
    assert result.passed


async def test_rate_limit_at_cap_fails(db):
    account = await _make_account(db)
    for _ in range(10):
        db.add(
            Order(
                account_id=account.id, ticker="AAPL", side="BUY", order_type="MARKET",
                qty=1, remaining_qty=1, status="REJECTED",
            )
        )
    await db.flush()
    result = await check_rate_limit(account, db)
    assert not result.passed
    assert result.reason_code == "RATE_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# check_wash_trade (never blocks -- passed=False just means "flagged")
# ---------------------------------------------------------------------------
async def test_wash_trade_no_resting_opposite_order_passes(db):
    account = await _make_account(db)
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    result = await check_wash_trade(order, account, db)
    assert result.passed


async def test_wash_trade_resting_opposite_order_flags(db):
    account = await _make_account(db)
    db.add(
        Order(
            account_id=account.id, ticker="AAPL", side="SELL", order_type="LIMIT",
            qty=1, remaining_qty=1, limit_price=Decimal("240.00"), status="ROUTED",
        )
    )
    await db.flush()
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    result = await check_wash_trade(order, account, db)
    assert not result.passed
    assert result.reason_code == "WASH_TRADE_FLAG"


# ---------------------------------------------------------------------------
# check_mis_margin
# ---------------------------------------------------------------------------
async def test_mis_margin_not_applicable_to_cnc(db):
    account = await _make_account(db, cash="1.00")
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1000, product_type="CNC")
    result = await check_mis_margin(order, Decimal("233.00"), account, db)
    assert result.passed


async def test_mis_margin_sufficient_passes(db):
    account = await _make_account(db, cash="1000000.00")
    order = _order(ticker="WMT", side="BUY", type="MARKET", qty=10, product_type="MIS")
    result = await check_mis_margin(order, Decimal("70.41"), account, db)
    assert result.passed


async def test_mis_margin_insufficient_fails(db):
    # This is the scenario that's structurally unreachable through the full
    # HTTP chain (see module docstring): a low-cash account makes it the
    # binding constraint instead of the notional cap.
    account = await _make_account(db, cash="100.00")
    order = _order(ticker="WMT", side="BUY", type="MARKET", qty=10, product_type="MIS")
    result = await check_mis_margin(order, Decimal("70.41"), account, db)
    assert not result.passed
    assert result.reason_code == "INSUFFICIENT_MARGIN"


async def test_mis_margin_short_sell_uses_collateral_multiplier(db):
    account = await _make_account(db, cash="100.00")
    order = _order(ticker="WMT", side="SELL", type="MARKET", qty=10, product_type="MIS")
    result = await check_mis_margin(order, Decimal("70.41"), account, db)
    assert not result.passed
    assert result.reason_code == "INSUFFICIENT_MARGIN"


# ---------------------------------------------------------------------------
# validate_order: chain-level behavior (short-circuit, wash trade non-block)
# ---------------------------------------------------------------------------
async def test_validate_order_happy_path(db):
    account = await _make_account(db)
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    outcome = await validate_order(order, account, db, reference_time=MARKET_OPEN)
    assert outcome.passed
    assert outcome.reason_code is None
    assert outcome.current_price is not None
    assert set(outcome.checks) == {
        "kyc_approved", "ticker_valid", "market_hours", "price_collar", "notional_limit",
        "concentration_limit", "buying_power", "rate_limit", "wash_trade",
        "sl_configuration", "mis_margin", "mis_square_off_window",
    }


async def test_validate_order_short_circuits_on_first_failure(db):
    account = Account(kyc_status="NOT_STARTED")  # not persisted -- fails at check 0, no DB access needed
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    outcome = await validate_order(order, account, db, reference_time=MARKET_OPEN)
    assert not outcome.passed
    assert outcome.reason_code == "KYC_NOT_APPROVED"
    # only the one check that ran should be recorded -- confirms the chain
    # actually stops, not just that the final result is a failure
    assert list(outcome.checks) == ["kyc_approved"]


async def test_validate_order_wash_trade_flags_but_does_not_block(db):
    account = await _make_account(db)
    db.add(
        Order(
            account_id=account.id, ticker="AAPL", side="SELL", order_type="LIMIT",
            qty=1, remaining_qty=1, limit_price=Decimal("240.00"), status="ROUTED",
        )
    )
    await db.flush()
    order = _order(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    outcome = await validate_order(order, account, db, reference_time=MARKET_OPEN)
    assert outcome.passed  # never blocks
    assert outcome.wash_trade_flagged
    assert outcome.wash_trade_message is not None
