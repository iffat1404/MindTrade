"""Task 3.6: unit tests for the order-engine checks that need no DB access
(kyc, ticker, market hours, price collar, notional limit, SL configuration,
MIS square-off window). DB-backed checks and full-chain integration tests
live in test_order_validation.py.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models.orm import Account
from app.models.schemas import OrderCreateRequest
from app.services import order_engine as oe

MARKET_OPEN = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)  # well within 09:30-16:00 UTC
BEFORE_OPEN = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
AT_CLOSE = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)  # close is an exclusive upper bound
AT_OPEN = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)  # open is an inclusive lower bound


def _order(**kw) -> OrderCreateRequest:
    defaults = dict(ticker="AAPL", side="BUY", type="MARKET", qty=1)
    defaults.update(kw)
    return OrderCreateRequest(**defaults)


# ---------------------------------------------------------------------------
# check_kyc_approved
# ---------------------------------------------------------------------------
def test_kyc_approved_passes():
    account = Account(kyc_status="APPROVED")
    assert oe.check_kyc_approved(account).passed


@pytest.mark.parametrize("status", ["NOT_STARTED", "PENDING_REVIEW", "REJECTED"])
def test_kyc_not_approved_fails(status):
    account = Account(kyc_status=status)
    result = oe.check_kyc_approved(account)
    assert not result.passed
    assert result.reason_code == "KYC_NOT_APPROVED"


# ---------------------------------------------------------------------------
# check_ticker_valid
# ---------------------------------------------------------------------------
def test_ticker_valid_passes():
    assert oe.check_ticker_valid("AAPL").passed


def test_ticker_invalid_fails():
    result = oe.check_ticker_valid("ZZZZ")
    assert not result.passed
    assert result.reason_code == "INVALID_TICKER"


# ---------------------------------------------------------------------------
# check_market_hours
# ---------------------------------------------------------------------------
def test_market_hours_mid_session_passes():
    assert oe.check_market_hours(MARKET_OPEN).passed


def test_market_hours_before_open_fails():
    result = oe.check_market_hours(BEFORE_OPEN)
    assert not result.passed
    assert result.reason_code == "MARKET_CLOSED"


def test_market_hours_at_close_fails():
    # close (16:00) is an exclusive upper bound
    assert not oe.check_market_hours(AT_CLOSE).passed


def test_market_hours_at_open_passes():
    # open (09:30) is an inclusive lower bound
    assert oe.check_market_hours(AT_OPEN).passed


# ---------------------------------------------------------------------------
# check_price_collar
# ---------------------------------------------------------------------------
def test_price_collar_market_order_not_applicable():
    order = _order(type="MARKET")
    assert oe.check_price_collar(order, Decimal("233.00")).passed


def test_price_collar_limit_within_band_passes():
    order = _order(type="LIMIT", limit_price="235.00")
    assert oe.check_price_collar(order, Decimal("233.00")).passed


def test_price_collar_limit_outside_band_fails():
    order = _order(type="LIMIT", limit_price="1000.00")
    result = oe.check_price_collar(order, Decimal("233.00"))
    assert not result.passed
    assert result.reason_code == "PRICE_COLLAR_BREACH"


# ---------------------------------------------------------------------------
# check_notional_limit
# ---------------------------------------------------------------------------
def test_notional_limit_under_cap_passes():
    order = _order(type="MARKET", qty=10)
    assert oe.check_notional_limit(order, Decimal("233.00")).passed


def test_notional_limit_over_cap_fails():
    order = _order(type="MARKET", qty=100_000)
    result = oe.check_notional_limit(order, Decimal("233.00"))
    assert not result.passed
    assert result.reason_code == "NOTIONAL_LIMIT_EXCEEDED"


def test_notional_limit_uses_limit_price_for_limit_orders():
    # qty*limit_price is under the cap even though qty*current_price would not be
    order = _order(type="LIMIT", qty=10, limit_price="10.00")
    assert oe.check_notional_limit(order, Decimal("999999.00")).passed


# ---------------------------------------------------------------------------
# check_sl_configuration
# ---------------------------------------------------------------------------
def test_sl_configuration_not_applicable_to_market_and_limit():
    assert oe.check_sl_configuration(_order(type="MARKET"), Decimal("233")).passed
    assert oe.check_sl_configuration(_order(type="LIMIT", limit_price="233"), Decimal("233")).passed


def test_sl_configuration_buy_trigger_above_last_passes():
    order = _order(type="SL-M", side="BUY", stop_trigger_price="240.00")
    assert oe.check_sl_configuration(order, Decimal("233.00")).passed


def test_sl_configuration_buy_trigger_below_last_fails():
    order = _order(type="SL-M", side="BUY", stop_trigger_price="220.00")
    result = oe.check_sl_configuration(order, Decimal("233.00"))
    assert not result.passed
    assert result.reason_code == "INVALID_SL_CONFIGURATION"


def test_sl_configuration_sell_trigger_below_last_passes():
    order = _order(type="SL-M", side="SELL", stop_trigger_price="220.00")
    assert oe.check_sl_configuration(order, Decimal("233.00")).passed


def test_sl_configuration_sell_trigger_above_last_fails():
    order = _order(type="SL-M", side="SELL", stop_trigger_price="240.00")
    result = oe.check_sl_configuration(order, Decimal("233.00"))
    assert not result.passed
    assert result.reason_code == "INVALID_SL_CONFIGURATION"


def test_sl_configuration_buy_stop_limit_must_be_above_trigger():
    good = _order(type="SL", side="BUY", stop_trigger_price="240.00", stop_limit_price="241.00")
    bad = _order(type="SL", side="BUY", stop_trigger_price="240.00", stop_limit_price="239.00")
    assert oe.check_sl_configuration(good, Decimal("233.00")).passed
    assert not oe.check_sl_configuration(bad, Decimal("233.00")).passed


def test_sl_configuration_sell_stop_limit_must_be_below_trigger():
    good = _order(type="SL", side="SELL", stop_trigger_price="220.00", stop_limit_price="219.00")
    bad = _order(type="SL", side="SELL", stop_trigger_price="220.00", stop_limit_price="221.00")
    assert oe.check_sl_configuration(good, Decimal("233.00")).passed
    assert not oe.check_sl_configuration(bad, Decimal("233.00")).passed


# ---------------------------------------------------------------------------
# check_mis_square_off_window
# ---------------------------------------------------------------------------
def test_mis_square_off_not_applicable_to_cnc():
    order = _order(product_type="CNC")
    assert oe.check_mis_square_off_window(order, AT_CLOSE).passed


def test_mis_square_off_before_close_passes():
    order = _order(product_type="MIS")
    assert oe.check_mis_square_off_window(order, MARKET_OPEN).passed


def test_mis_square_off_at_close_fails():
    order = _order(product_type="MIS")
    result = oe.check_mis_square_off_window(order, AT_CLOSE)
    assert not result.passed
    assert result.reason_code == "MIS_SQUARE_OFF_WINDOW"
