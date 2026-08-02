import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import Account, CashLedger, Fill, Order, OrderEvent, OrderMatch, Position
from app.services import behavioral_guard
from app.services.portfolio_engine import apply_fill_to_position

logger = logging.getLogger("mindtrade.matching")


@dataclass
class TickBar:
    ticker: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _log_event(db: AsyncSession, order: Order, from_state: str, to_state: str, reason: str) -> None:
    db.add(OrderEvent(order_id=order.id, from_state=from_state, to_state=to_state, reason=reason))


_PRICE_QUANTUM = Decimal("0.0001")  # matches Fill/OrderMatch.fill_price's Numeric(10, 4)


def _synthetic_price(side: str, tick: TickBar) -> Decimal:
    """Synthetic bid/ask around the tick's close (Sprint 3's "fills at
    synthetic ask" for MARKET orders, extended here to any order falling
    back to synthetic liquidity instead of a real counterparty).

    Quantized to the same 4 decimal places as the fill_price columns it
    feeds -- otherwise this computes with more precision than ever
    actually gets persisted (e.g. 233.038245), and using the unrounded
    value for the cash-impact math produces a cash_balance that silently
    drifts by a fraction of a cent from what a fresh query of the
    (rounded) fill_price would imply.
    """
    half_spread = tick.close * settings.SYNTHETIC_SPREAD_BPS / Decimal("10000") / 2
    price = tick.close + half_spread if side == "BUY" else tick.close - half_spread
    return price.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def _sl_triggered(order: Order, tick: TickBar) -> bool:
    if order.side == "BUY":
        return tick.high >= order.stop_trigger_price
    return tick.low <= order.stop_trigger_price


def _limit_condition_met(side: str, price: Decimal, tick: TickBar) -> bool:
    """Whether the tick's range would have let a resting limit-type order
    at `price` fill against the market (used for both plain LIMIT orders
    and triggered SL orders, which become a limit at stop_limit_price).
    """
    return tick.low <= price if side == "BUY" else tick.high >= price


async def _get_resting_orders(db: AsyncSession, ticker: str, order_types: list[str]) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.ticker == ticker,
            Order.order_type.in_(order_types),
            Order.status == "ROUTED",
            Order.remaining_qty > 0,
        )
        .order_by(Order.created_at.asc())
    )
    return list((await db.scalars(stmt)).all())


async def _settle_fill(
    db: AsyncSession,
    order: Order,
    fill_qty: int,
    fill_price: Decimal,
    *,
    matched_at: datetime,
    match_type: str,
    matching_log: dict,
    counterparty_account_id: Optional[uuid.UUID] = None,
) -> None:
    """Records one fill against `order`: Fill row, order_matches audit row,
    remaining_qty/status update, order_events transition, position (via
    FIFO lots), cash ledger + account cash/margin.
    """
    db.add(
        Fill(
            order_id=order.id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            fees=settings.COMMISSION_FLAT_FEE,
            reason=match_type,
        )
    )
    db.add(
        OrderMatch(
            order_id=order.id,
            matched_at=matched_at,
            match_type=match_type,
            fill_price=fill_price,
            fill_qty=fill_qty,
            counterparty_account_id=counterparty_account_id,
            matching_log=matching_log,
        )
    )

    from_status = order.status
    order.remaining_qty -= fill_qty
    order.status = "FILLED" if order.remaining_qty <= 0 else "ROUTED"
    _log_event(
        db, order, from_status, order.status,
        f"Filled {fill_qty}@{fill_price} via {match_type} (remaining {order.remaining_qty})",
    )

    account = await db.get(Account, order.account_id)
    realized = await apply_fill_to_position(
        db,
        account_id=order.account_id,
        ticker=order.ticker,
        order_side=order.side,
        fill_qty=fill_qty,
        fill_price=fill_price,
        is_intraday=(order.product_type == "MIS"),
    )
    await behavioral_guard.record_fill_outcome(
        db,
        account_id=order.account_id,
        ticker=order.ticker,
        side=order.side,
        qty=fill_qty,
        price=fill_price,
        realized_pnl=realized,
        market_time=matched_at,
    )

    notional = fill_price * fill_qty
    if order.side == "BUY":
        cash_delta = -(notional + settings.COMMISSION_FLAT_FEE)
    else:
        cash_delta = notional - settings.COMMISSION_FLAT_FEE
    # Quantize to cash_balance's actual Numeric(15, 2) precision -- same
    # reasoning as _synthetic_price above: round at the point of ledger
    # recording rather than let sub-cent fractions silently accumulate.
    cash_delta = cash_delta.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    account.cash_balance += cash_delta
    db.add(CashLedger(account_id=order.account_id, amount=cash_delta, reason=f"FILL_{order.side}_{order.ticker}"))

    if order.product_type == "MIS":
        await _recompute_margin_used(db, account)

    logger.info(
        "Filled order %s: %s %d %s @ %s (%s), realized_pnl=%s",
        order.id, order.side, fill_qty, order.ticker, fill_price, match_type, realized,
    )


async def square_off_position(db: AsyncSession, position: Position, price: Decimal, as_of: datetime) -> None:
    """Force-closes a MIS position (EOD auto square-off, Task 4.4). Creates
    a real Order -- so it shows up in order history like any other trade,
    not as an invisible side effect -- immediately filled via
    _settle_fill, the same accounting path as every other fill.
    """
    side = "SELL" if position.signed_qty > 0 else "BUY"
    qty = abs(position.signed_qty)

    order = Order(
        account_id=position.account_id, ticker=position.ticker, side=side, order_type="MARKET",
        product_type="MIS", qty=qty, remaining_qty=qty, status="ROUTED",
    )
    db.add(order)
    await db.flush()
    _log_event(db, order, None, "NEW", "EOD auto square-off")
    _log_event(db, order, "NEW", "ROUTED", "EOD auto square-off")

    log = {
        "tick_timestamp": as_of.isoformat(),
        "order_id": str(order.id),
        "side": side,
        "order_type": "MARKET",
        "fill_price": str(price),
        "priority_reason": f"EOD auto square-off at {settings.MIS_SQUARE_OFF_TIME_UTC} UTC",
        "algorithm": "NSE_FIFO_PriceTime_v1",
    }
    await _settle_fill(db, order, qty, price, matched_at=as_of, match_type="EOD_SQUARE_OFF", matching_log=log)


async def _recompute_margin_used(db: AsyncSession, account: Account) -> None:
    """Recomputes margin_used from scratch as the total capital required
    to hold all of the account's current MIS positions, rather than
    incrementally reserving/releasing per fill -- simpler to keep correct
    (idempotent) than tracking exactly how much was reserved per lot.
    """
    positions = (
        await db.scalars(
            select(Position).where(
                Position.account_id == account.id,
                Position.is_intraday.is_(True),
                Position.signed_qty != 0,
            )
        )
    ).all()

    total = Decimal("0")
    for p in positions:
        multiplier = settings.SHORT_COLLATERAL_MULTIPLIER if p.signed_qty < 0 else settings.MIS_MARGIN_MULTIPLIER
        total += abs(p.signed_qty) * p.avg_cost * multiplier

    account.margin_used = total


async def process_tick(db: AsyncSession, tick: TickBar) -> None:
    """Processes one simulated minute bar for one ticker: triggers SL/SL-M
    orders, matches crossing LIMIT orders against each other (real
    trader-vs-trader NSE FIFO price-time priority, match_type=LIMIT_CROSS),
    then fills anything left (MARKET orders, and LIMIT/triggered-SL orders
    with no natural counterparty) against synthetic liquidity
    (match_type=SYNTHETIC_BID/SYNTHETIC_ASK).
    """
    market_orders = await _get_resting_orders(db, tick.ticker, ["MARKET"])
    limit_orders = await _get_resting_orders(db, tick.ticker, ["LIMIT"])
    stop_orders = await _get_resting_orders(db, tick.ticker, ["SL", "SL-M"])

    triggered_sl_m: list[Order] = []
    triggered_sl_as_limit: list[Order] = []
    for order in stop_orders:
        if _sl_triggered(order, tick):
            if order.order_type == "SL-M":
                triggered_sl_m.append(order)
            else:
                triggered_sl_as_limit.append(order)

    # --- Phase 1: real trader-vs-trader matching (LIMIT_CROSS) ---
    # Triggered SL orders participate as limit orders at stop_limit_price.
    tradeable = [(o, o.limit_price) for o in limit_orders] + [(o, o.stop_limit_price) for o in triggered_sl_as_limit]
    buys = sorted((o for o, p in tradeable if o.side == "BUY"), key=lambda o: (-((o.limit_price or o.stop_limit_price)), o.created_at))
    sells = sorted((o for o, p in tradeable if o.side == "SELL"), key=lambda o: ((o.limit_price or o.stop_limit_price), o.created_at))

    # Orders that get any fill here (even partial) are excluded from phase
    # 2's synthetic top-up below: once a resting order has actually traded
    # against a real counterparty this tick, real exchanges don't also let
    # it eat through "infinite" synthetic liquidity for its leftover qty --
    # the remainder just keeps resting for a future tick's opportunity.
    matched_this_tick: set[uuid.UUID] = set()

    bi, si = 0, 0
    while bi < len(buys) and si < len(sells):
        buy = buys[bi]
        sell = sells[si]
        buy_price = buy.limit_price if buy.limit_price is not None else buy.stop_limit_price
        sell_price = sell.limit_price if sell.limit_price is not None else sell.stop_limit_price

        if buy_price < sell_price:
            break  # best remaining bid can't cross best remaining ask

        resting, aggressor = (buy, sell) if buy.created_at <= sell.created_at else (sell, buy)
        match_price = buy_price if resting is buy else sell_price
        fill_qty = min(buy.remaining_qty, sell.remaining_qty)

        log = {
            "tick_timestamp": tick.timestamp.isoformat(),
            "buy_order_id": str(buy.id),
            "sell_order_id": str(sell.id),
            "buy_price": str(buy_price),
            "sell_price": str(sell_price),
            "match_price": str(match_price),
            "fill_qty": fill_qty,
            "buy_created": buy.created_at.isoformat(),
            "sell_created": sell.created_at.isoformat(),
            "priority_reason": (
                f"{'Buy' if resting is buy else 'Sell'} order resting first "
                f"({resting.created_at.isoformat()}) sets the match price"
            ),
            "algorithm": "NSE_FIFO_PriceTime_v1",
        }

        await _settle_fill(
            db, buy, fill_qty, match_price, matched_at=tick.timestamp, match_type="LIMIT_CROSS",
            matching_log=log, counterparty_account_id=sell.account_id,
        )
        await _settle_fill(
            db, sell, fill_qty, match_price, matched_at=tick.timestamp, match_type="LIMIT_CROSS",
            matching_log=log, counterparty_account_id=buy.account_id,
        )
        matched_this_tick.add(buy.id)
        matched_this_tick.add(sell.id)

        if buy.remaining_qty <= 0:
            bi += 1
        if sell.remaining_qty <= 0:
            si += 1

    # --- Phase 2: synthetic fills for whatever's left ---
    for order in market_orders:
        price = _synthetic_price(order.side, tick)
        log = {
            "tick_timestamp": tick.timestamp.isoformat(),
            "order_id": str(order.id),
            "side": order.side,
            "order_type": "MARKET",
            "tick_close": str(tick.close),
            "fill_price": str(price),
            "priority_reason": "MARKET order filled immediately at synthetic bid/ask",
            "algorithm": "NSE_FIFO_PriceTime_v1",
        }
        await _settle_fill(
            db, order, order.remaining_qty, price, matched_at=tick.timestamp,
            match_type="SYNTHETIC_ASK" if order.side == "BUY" else "SYNTHETIC_BID", matching_log=log,
        )

    for order in triggered_sl_m:
        price = _synthetic_price(order.side, tick)
        log = {
            "tick_timestamp": tick.timestamp.isoformat(),
            "order_id": str(order.id),
            "side": order.side,
            "order_type": "SL-M",
            "stop_trigger_price": str(order.stop_trigger_price),
            "tick_high": str(tick.high),
            "tick_low": str(tick.low),
            "fill_price": str(price),
            "priority_reason": "SL-M triggered this tick, filled at synthetic bid/ask",
            "algorithm": "NSE_FIFO_PriceTime_v1",
        }
        await _settle_fill(
            db, order, order.remaining_qty, price, matched_at=tick.timestamp,
            match_type="SYNTHETIC_ASK" if order.side == "BUY" else "SYNTHETIC_BID", matching_log=log,
        )

    for order in limit_orders + triggered_sl_as_limit:
        if order.id in matched_this_tick:
            continue  # already had a real (even if partial) match this tick
        price = order.limit_price if order.limit_price is not None else order.stop_limit_price
        if not _limit_condition_met(order.side, price, tick):
            continue  # market didn't reach this price this tick -- stays resting
        log = {
            "tick_timestamp": tick.timestamp.isoformat(),
            "order_id": str(order.id),
            "side": order.side,
            "order_type": order.order_type,
            "limit_price": str(price),
            "tick_high": str(tick.high),
            "tick_low": str(tick.low),
            "fill_price": str(price),
            "priority_reason": "No crossing counterparty; tick range reached the order's price",
            "algorithm": "NSE_FIFO_PriceTime_v1",
        }
        await _settle_fill(
            db, order, order.remaining_qty, price, matched_at=tick.timestamp,
            match_type="SYNTHETIC_ASK" if order.side == "BUY" else "SYNTHETIC_BID", matching_log=log,
        )
