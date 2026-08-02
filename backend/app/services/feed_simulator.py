import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import FeedState, Position, PriceHistoryMinute
from app.services.order_engine import get_last_price
from app.services.order_matching import TickBar, process_tick, square_off_position

logger = logging.getLogger("mindtrade.feed")

_FEED_STATE_ID = True  # singleton row


async def get_feed_state(db: AsyncSession) -> FeedState:
    state = await db.get(FeedState, _FEED_STATE_ID)
    if state is None:
        state = FeedState(
            id=_FEED_STATE_ID, current_tick_time=None, is_running=False,
            speed_multiplier=settings.FEED_DEFAULT_SPEED_MULTIPLIER,
        )
        db.add(state)
        await db.commit()
        await db.refresh(state)
    return state


async def start_feed(db: AsyncSession) -> FeedState:
    state = await get_feed_state(db)
    state.is_running = True
    await db.commit()
    return state


async def pause_feed(db: AsyncSession) -> FeedState:
    state = await get_feed_state(db)
    state.is_running = False
    await db.commit()
    return state


async def reset_feed(db: AsyncSession) -> FeedState:
    """"Reset to start date" (per FRONTEND_DESIGN_GUIDE's Feed Control
    page): rewinds the simulated clock to the beginning and pauses.
    Doesn't touch orders/positions/fills -- that's a separate, more
    destructive "clear all" action the docs mention but don't require for
    Sprint 4 (only status + reset are explicitly tasked).
    """
    state = await get_feed_state(db)
    state.current_tick_time = None
    state.is_running = False
    await db.commit()
    return state


def _is_last_tick_of_day(tick_time: datetime) -> bool:
    """Whether `tick_time` is the last bar of its trading day, used to
    trigger EOD MIS square-off. The loaded minute data always ends each
    day at 15:59 UTC (no 16:00 bar exists to compare against
    settings.MIS_SQUARE_OFF_TIME_UTC directly), so this checks for that
    specific, verified shape of the data rather than the nominal close time.
    """
    return tick_time.hour == 15 and tick_time.minute == 59


async def _square_off_all_mis(db: AsyncSession, as_of: datetime) -> None:
    positions = (
        await db.scalars(
            select(Position).where(Position.is_intraday.is_(True), Position.signed_qty != 0)
        )
    ).all()
    for position in positions:
        price = await get_last_price(db, position.ticker) or position.avg_cost
        await square_off_position(db, position, price, as_of)
        logger.info(
            "EOD square-off: account %s %s %d %s @ %s",
            position.account_id, "SELL" if position.signed_qty > 0 else "BUY",
            abs(position.signed_qty), position.ticker, price,
        )


async def _advance_one_tick(db: AsyncSession) -> Optional[datetime]:
    """Advances the feed to the next available simulated minute, processes
    it for every ticker that has a bar at that timestamp, and runs EOD
    square-off if it's the last tick of the day. Returns the new
    current_tick_time, or None if the loaded data is exhausted.
    """
    state = await get_feed_state(db)

    if state.current_tick_time is None:
        next_time = await db.scalar(select(func.min(PriceHistoryMinute.timestamp)))
    else:
        next_time = await db.scalar(
            select(func.min(PriceHistoryMinute.timestamp)).where(
                PriceHistoryMinute.timestamp > state.current_tick_time
            )
        )

    if next_time is None:
        state.is_running = False
        await db.commit()
        return None

    state.current_tick_time = next_time
    await db.flush()

    bars = (
        await db.scalars(select(PriceHistoryMinute).where(PriceHistoryMinute.timestamp == next_time))
    ).all()
    for bar in bars:
        tick = TickBar(
            ticker=bar.ticker, timestamp=bar.timestamp, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume,
        )
        await process_tick(db, tick)

    if _is_last_tick_of_day(next_time):
        await _square_off_all_mis(db, next_time)

    await db.commit()
    return next_time


async def run_feed_loop(session_factory: Callable[[], AsyncSession], *, stop_event: Optional[asyncio.Event] = None) -> None:
    """The feed simulator's background task (Task 4.4): advances one
    simulated minute at a time while running, sleeping between ticks
    according to speed_multiplier, until the loaded data is exhausted or
    the app shuts down. Polls (rather than blocking) while paused, so an
    admin resuming mid-run takes effect within ~1 second.
    """
    logger.info("Feed simulator background task starting")
    while stop_event is None or not stop_event.is_set():
        async with session_factory() as db:
            state = await get_feed_state(db)
            if not state.is_running:
                await db.commit()
                await asyncio.sleep(1)
                continue
            speed = max(state.speed_multiplier, 1)
            next_time = await _advance_one_tick(db)

        if next_time is None:
            await asyncio.sleep(1)
            continue

        await asyncio.sleep(settings.FEED_BASE_TICK_INTERVAL_SECONDS / speed)
    logger.info("Feed simulator background task stopped")
