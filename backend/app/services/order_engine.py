from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import Account, Order, Position, PriceHistoryDaily, PriceHistoryMinute
from app.models.schemas import OrderCreateRequest


@dataclass
class ValidationResult:
    passed: bool
    reason_code: Optional[str] = None
    message: Optional[str] = None


@dataclass
class OrderValidationOutcome:
    passed: bool
    reason_code: Optional[str] = None
    message: Optional[str] = None
    current_price: Optional[Decimal] = None
    # Wash trade never blocks (see check_wash_trade below), so its result is
    # surfaced separately rather than via passed/reason_code.
    wash_trade_flagged: bool = False
    wash_trade_message: Optional[str] = None
    checks: dict[str, ValidationResult] = field(default_factory=dict)


def _parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


async def get_last_price(db: AsyncSession, ticker: str) -> Optional[Decimal]:
    """Latest available close for `ticker`. Sprint 3 has no live feed yet
    (the feed simulator is Sprint 4), so "current price" is the most recent
    daily close -- Sprint 4 supersedes this with real tick data once it
    exists; the signature (db, ticker) -> price stays the same either way.
    """
    stmt = (
        select(PriceHistoryDaily.close)
        .where(PriceHistoryDaily.ticker == ticker)
        .order_by(PriceHistoryDaily.date.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def get_latest_market_time(db: AsyncSession) -> Optional[datetime]:
    """Latest timestamp across all loaded minute bars -- used as "now" for
    market-hours/MIS-square-off checks until the feed simulator (Sprint 4)
    provides a live simulated clock.

    This platform replays a fixed simulated period (Jun 30-Aug 29, 2026);
    real wall-clock time has no relationship to that calendar and using it
    directly would make order placement randomly fail with MARKET_CLOSED
    depending purely on what time of day someone happens to run the demo.
    The latest loaded minute-bar timestamp is always within trading hours
    by construction (that's what the data represents), so it's a much
    better stand-in "now" than datetime.now() until Sprint 4 exists.
    """
    return await db.scalar(select(func.max(PriceHistoryMinute.timestamp)))


# ---------------------------------------------------------------------------
# The 12 checks (0-11), per HOW_TO_USE_MASTER_BUILD_PLAN.md's example and
# MASTER_BUILD_PLAN_2.md Sprint 3. All except check_wash_trade short-circuit
# the chain on failure.
# ---------------------------------------------------------------------------


def check_kyc_approved(account: Account) -> ValidationResult:
    if account.kyc_status != "APPROVED":
        return ValidationResult(False, "KYC_NOT_APPROVED", "Your KYC is not yet approved")
    return ValidationResult(True)


def check_ticker_valid(ticker: str) -> ValidationResult:
    if ticker not in settings.TICKERS:
        return ValidationResult(False, "INVALID_TICKER", f"{ticker} is not a tradeable ticker")
    return ValidationResult(True)


def check_market_hours(reference_time: datetime) -> ValidationResult:
    open_t = _parse_hhmm(settings.MARKET_OPEN_TIME_UTC)
    close_t = _parse_hhmm(settings.MARKET_CLOSE_TIME_UTC)
    now_t = reference_time.astimezone(timezone.utc).time()
    if not (open_t <= now_t < close_t):
        return ValidationResult(
            False,
            "MARKET_CLOSED",
            f"Market is closed (open {settings.MARKET_OPEN_TIME_UTC}-{settings.MARKET_CLOSE_TIME_UTC} UTC)",
        )
    return ValidationResult(True)


def check_price_collar(order: OrderCreateRequest, current_price: Decimal) -> ValidationResult:
    # Only meaningful for LIMIT orders (a user-chosen price to sanity-check
    # against the market). MARKET orders execute at the synthetic
    # bid/ask by definition; SL/SL-M trigger prices are intentionally far
    # from the current price and are validated by check_sl_configuration
    # instead.
    if order.order_type != "LIMIT":
        return ValidationResult(True)

    deviation = abs(order.limit_price - current_price) / current_price
    if deviation > settings.PRICE_COLLAR_PCT:
        return ValidationResult(
            False,
            "PRICE_COLLAR_BREACH",
            f"Limit price {order.limit_price} is {deviation:.1%} from last price {current_price} "
            f"(max {settings.PRICE_COLLAR_PCT:.0%})",
        )
    return ValidationResult(True)


def check_notional_limit(order: OrderCreateRequest, current_price: Decimal) -> ValidationResult:
    price = order.limit_price if order.order_type == "LIMIT" else current_price
    notional = price * order.qty
    if notional > settings.MAX_NOTIONAL_PER_ORDER:
        return ValidationResult(
            False,
            "NOTIONAL_LIMIT_EXCEEDED",
            f"Order notional {notional} exceeds the max of {settings.MAX_NOTIONAL_PER_ORDER}",
        )
    return ValidationResult(True)


async def check_concentration_limit(
    order: OrderCreateRequest, current_price: Decimal, account: Account, db: AsyncSession
) -> ValidationResult:
    positions = (
        await db.scalars(
            select(Position).where(
                Position.account_id == account.id,
                Position.is_backtest.is_(False),
            )
        )
    ).all()

    net_worth = account.cash_balance
    existing_ticker_value = Decimal("0")
    for p in positions:
        p_price = current_price if p.ticker == order.ticker else (await get_last_price(db, p.ticker) or Decimal("0"))
        value = p.signed_qty * p_price
        net_worth += value
        if p.ticker == order.ticker:
            existing_ticker_value += value

    if net_worth <= 0:
        return ValidationResult(
            False, "CONCENTRATION_LIMIT_EXCEEDED", "Cannot evaluate concentration against non-positive net worth"
        )

    order_price = order.limit_price if order.order_type == "LIMIT" else current_price
    signed_order_value = order_price * order.qty * (1 if order.side == "BUY" else -1)
    projected_value = abs(existing_ticker_value + signed_order_value)

    concentration = projected_value / net_worth
    if concentration > settings.MAX_CONCENTRATION_PCT:
        return ValidationResult(
            False,
            "CONCENTRATION_LIMIT_EXCEEDED",
            f"This order would put {concentration:.1%} of net worth in {order.ticker} "
            f"(max {settings.MAX_CONCENTRATION_PCT:.0%})",
        )
    return ValidationResult(True)


async def check_buying_power(
    order: OrderCreateRequest, current_price: Decimal, account: Account, db: AsyncSession
) -> ValidationResult:
    # MIS capital requirements (both directions) are check_mis_margin's job,
    # not this check's -- avoids the same requirement being validated twice
    # under two different reason codes.
    if order.product_type == "MIS":
        return ValidationResult(True)

    order_price = order.limit_price if order.order_type == "LIMIT" else current_price
    notional = order_price * order.qty

    if order.side == "BUY":
        required = notional + settings.COMMISSION_FLAT_FEE
        available = account.cash_balance - account.margin_used
        if available < required:
            return ValidationResult(
                False, "INSUFFICIENT_BUYING_POWER", f"Order requires {required}, {available} available"
            )
        return ValidationResult(True)

    # CNC SELL: a delivery product, so no naked shorting -- must actually
    # hold enough shares.
    position = await db.scalar(
        select(Position).where(
            Position.account_id == account.id,
            Position.ticker == order.ticker,
            Position.is_backtest.is_(False),
            Position.is_intraday.is_(False),
        )
    )
    held = position.signed_qty if position is not None else 0
    if held < order.qty:
        return ValidationResult(
            False,
            "INSUFFICIENT_HOLDINGS",
            f"You hold {held} shares of {order.ticker}; cannot sell {order.qty} (CNC requires ownership)",
        )
    return ValidationResult(True)


async def check_rate_limit(account: Account, db: AsyncSession) -> ValidationResult:
    window_start = datetime.now(timezone.utc) - timedelta(seconds=60)
    count = await db.scalar(
        select(func.count()).select_from(Order).where(Order.account_id == account.id, Order.created_at >= window_start)
    )
    if count is not None and count >= settings.ORDER_RATE_LIMIT_PER_MINUTE:
        return ValidationResult(
            False,
            "RATE_LIMIT_EXCEEDED",
            f"{count} orders placed in the last minute (max {settings.ORDER_RATE_LIMIT_PER_MINUTE})",
        )
    return ValidationResult(True)


async def check_wash_trade(order: OrderCreateRequest, account: Account, db: AsyncSession) -> ValidationResult:
    """Never blocks the order -- flags a potential wash trade (a resting
    opposite-side order for the same ticker on the same account) for audit,
    per MASTER_BUILD_PLAN's "flags but doesn't reject" rule.
    """
    opposite_side = "SELL" if order.side == "BUY" else "BUY"
    existing = await db.scalar(
        select(Order)
        .where(
            Order.account_id == account.id,
            Order.ticker == order.ticker,
            Order.side == opposite_side,
            Order.status.in_(["VALIDATED", "ROUTED"]),
        )
        .limit(1)
    )
    if existing is not None:
        return ValidationResult(
            False,
            "WASH_TRADE_FLAG",
            f"Resting {opposite_side} order for {order.ticker} exists on this account -- potential wash trade",
        )
    return ValidationResult(True)


def check_sl_configuration(order: OrderCreateRequest, current_price: Decimal) -> ValidationResult:
    """Validates SL/SL-M trigger direction and (for SL) trigger vs. limit.

    NOTE: HOW_TO_USE_MASTER_BUILD_PLAN.md's Task 3.2 literally says "BUY
    trigger < last, SELL trigger > last" -- that's backwards from how stop
    orders actually work (and from NSE/Zerodha's real convention): a BUY
    stop triggers on a price *rise* (breakout, or covering a short before
    losses grow), so its trigger must be ABOVE the last price; a SELL stop
    triggers on a price *fall* (protecting a long), so its trigger must be
    BELOW it. Implemented correctly here rather than per the doc's literal
    (backwards) text; see also the doc fix in the same commit.
    """
    if order.order_type not in ("SL", "SL-M"):
        return ValidationResult(True)

    trigger = order.stop_trigger_price
    if order.side == "BUY" and trigger <= current_price:
        return ValidationResult(
            False,
            "INVALID_SL_CONFIGURATION",
            f"BUY stop trigger ({trigger}) must be above the last price ({current_price})",
        )
    if order.side == "SELL" and trigger >= current_price:
        return ValidationResult(
            False,
            "INVALID_SL_CONFIGURATION",
            f"SELL stop trigger ({trigger}) must be below the last price ({current_price})",
        )

    if order.order_type == "SL":
        # stop_limit_price sits on the far side of the trigger (in the
        # order's direction), giving some slippage room once triggered.
        if order.side == "BUY" and order.stop_limit_price < trigger:
            return ValidationResult(
                False, "INVALID_SL_CONFIGURATION", "BUY stop_limit_price must be >= stop_trigger_price"
            )
        if order.side == "SELL" and order.stop_limit_price > trigger:
            return ValidationResult(
                False, "INVALID_SL_CONFIGURATION", "SELL stop_limit_price must be <= stop_trigger_price"
            )

    return ValidationResult(True)


async def check_mis_margin(
    order: OrderCreateRequest, current_price: Decimal, account: Account, db: AsyncSession
) -> ValidationResult:
    if order.product_type != "MIS":
        return ValidationResult(True)

    order_price = order.limit_price if order.order_type == "LIMIT" else current_price
    notional = order_price * order.qty
    # Per FRONTEND_DESIGN_GUIDE's order form ("Margin required (150%)"):
    # this demo platform requires MORE collateral for MIS than the plain
    # notional, not real-world intraday leverage -- implemented as
    # documented rather than modeling real broker margin rules.
    multiplier = settings.SHORT_COLLATERAL_MULTIPLIER if order.side == "SELL" else settings.MIS_MARGIN_MULTIPLIER
    required = notional * multiplier

    available = account.cash_balance - account.margin_used
    if available < required:
        return ValidationResult(
            False, "INSUFFICIENT_MARGIN", f"MIS order requires {required} margin, {available} available"
        )
    return ValidationResult(True)


def check_mis_square_off_window(order: OrderCreateRequest, reference_time: datetime) -> ValidationResult:
    if order.product_type != "MIS":
        return ValidationResult(True)

    square_off_t = _parse_hhmm(settings.MIS_SQUARE_OFF_TIME_UTC)
    now_t = reference_time.astimezone(timezone.utc).time()
    if now_t >= square_off_t:
        return ValidationResult(
            False,
            "MIS_SQUARE_OFF_WINDOW",
            f"Cannot open new MIS positions at/after {settings.MIS_SQUARE_OFF_TIME_UTC} UTC (auto square-off)",
        )
    return ValidationResult(True)


async def validate_order(
    order: OrderCreateRequest,
    account: Account,
    db: AsyncSession,
    *,
    reference_time: Optional[datetime] = None,
) -> OrderValidationOutcome:
    """Runs the full 12-check chain, short-circuiting on the first failure
    except check_wash_trade (flags but never blocks). Pure validation --
    does not create the Order row or touch order_events; that's the API
    layer's job (see api/orders.py), which also owns fill logic (Sprint 4).
    """
    reference_time = reference_time or datetime.now(timezone.utc)
    checks: dict[str, ValidationResult] = {}

    def _outcome(passed: bool, reason_code: Optional[str] = None, message: Optional[str] = None) -> OrderValidationOutcome:
        wash = checks.get("wash_trade")
        return OrderValidationOutcome(
            passed=passed,
            reason_code=reason_code,
            message=message,
            current_price=current_price,
            wash_trade_flagged=bool(wash and not wash.passed),
            wash_trade_message=wash.message if wash and not wash.passed else None,
            checks=checks,
        )

    current_price: Optional[Decimal] = None

    checks["kyc_approved"] = r = check_kyc_approved(account)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["ticker_valid"] = r = check_ticker_valid(order.ticker)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["market_hours"] = r = check_market_hours(reference_time)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    current_price = await get_last_price(db, order.ticker)
    if current_price is None:
        return _outcome(False, "NO_PRICE_DATA", f"No price data available for {order.ticker}")

    checks["price_collar"] = r = check_price_collar(order, current_price)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["notional_limit"] = r = check_notional_limit(order, current_price)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["concentration_limit"] = r = await check_concentration_limit(order, current_price, account, db)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["buying_power"] = r = await check_buying_power(order, current_price, account, db)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["rate_limit"] = r = await check_rate_limit(account, db)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["wash_trade"] = await check_wash_trade(order, account, db)  # never blocks

    checks["sl_configuration"] = r = check_sl_configuration(order, current_price)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["mis_margin"] = r = await check_mis_margin(order, current_price, account, db)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    checks["mis_square_off_window"] = r = check_mis_square_off_window(order, reference_time)
    if not r.passed:
        return _outcome(False, r.reason_code, r.message)

    return _outcome(True)
