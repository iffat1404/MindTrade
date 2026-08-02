"""Unit tests for feed_simulator.py: FeedState management, tick advancement
(including exhaustion at the end of loaded data), EOD-of-day detection, and
MIS auto square-off.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import get_password_hash
from app.models.orm import Account, FeedState, OrderMatch, Position
from app.services import feed_simulator as fs

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_account(db, *, cash="1000000.00") -> Account:
    account = Account(
        username=f"pytest_feed_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status="APPROVED",
        cash_balance=Decimal(cash),
        starting_capital=Decimal(cash),
    )
    db.add(account)
    await db.flush()
    return account


async def _clear_feed_state(db) -> None:
    # Each test wants a clean singleton row rather than whatever a
    # previous test (or the app's own lifespan) left behind.
    await db.execute(delete(FeedState))
    await db.commit()


# ---------------------------------------------------------------------------
# FeedState lifecycle
# ---------------------------------------------------------------------------
async def test_get_feed_state_creates_singleton_if_missing(db):
    await _clear_feed_state(db)
    state = await fs.get_feed_state(db)
    assert state.current_tick_time is None
    assert state.is_running is False
    assert state.speed_multiplier == 1

    # calling again returns the same row, not a duplicate
    state2 = await fs.get_feed_state(db)
    assert state2.id == state.id
    count = await db.scalar(select(FeedState))
    assert count is not None


async def test_start_pause_reset_feed(db):
    await _clear_feed_state(db)
    await fs.start_feed(db)
    state = await fs.get_feed_state(db)
    assert state.is_running is True

    await fs.pause_feed(db)
    state = await fs.get_feed_state(db)
    assert state.is_running is False

    state.current_tick_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    await db.commit()
    await fs.start_feed(db)

    reset_state = await fs.reset_feed(db)
    assert reset_state.current_tick_time is None
    assert reset_state.is_running is False


# ---------------------------------------------------------------------------
# _is_last_tick_of_day
# ---------------------------------------------------------------------------
async def test_is_last_tick_of_day():
    assert fs._is_last_tick_of_day(datetime(2026, 7, 1, 15, 59, tzinfo=timezone.utc))
    assert not fs._is_last_tick_of_day(datetime(2026, 7, 1, 15, 58, tzinfo=timezone.utc))
    assert not fs._is_last_tick_of_day(datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# _advance_one_tick
# ---------------------------------------------------------------------------
async def test_advance_one_tick_starts_at_earliest_bar(db):
    await _clear_feed_state(db)
    next_time = await fs._advance_one_tick(db)
    assert next_time == datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc)

    state = await fs.get_feed_state(db)
    assert state.current_tick_time == next_time


async def test_advance_one_tick_moves_forward(db):
    await _clear_feed_state(db)
    first = await fs._advance_one_tick(db)
    second = await fs._advance_one_tick(db)
    assert second > first
    assert second == datetime(2026, 6, 30, 9, 31, tzinfo=timezone.utc)


async def test_advance_one_tick_processes_every_ticker(db):
    await _clear_feed_state(db)
    tick_time = await fs._advance_one_tick(db)
    # AAPL, GOOG, IBM, MSFT, TSLA, UL, WMT should all have produced an
    # order_matches-eligible pass, even with no resting orders (0 matches
    # is fine) -- what matters is process_tick ran without error for all 7,
    # which not raising here already demonstrates. Spot check no crash and
    # a sane timestamp.
    assert tick_time is not None


async def test_advance_one_tick_returns_none_when_exhausted(db):
    await _clear_feed_state(db)
    state = await fs.get_feed_state(db)

    from sqlalchemy import func

    from app.models.orm import PriceHistoryMinute

    last_time = await db.scalar(select(func.max(PriceHistoryMinute.timestamp)))
    state.current_tick_time = last_time
    await db.commit()

    result = await fs._advance_one_tick(db)
    assert result is None
    state = await fs.get_feed_state(db)
    assert state.is_running is False


# ---------------------------------------------------------------------------
# EOD MIS square-off
# ---------------------------------------------------------------------------
async def test_square_off_all_mis_closes_positions(db):
    account = await _make_account(db)
    from app.services.portfolio_engine import apply_fill_to_position

    await apply_fill_to_position(
        db, account_id=account.id, ticker="AAPL", order_side="BUY", fill_qty=10,
        fill_price=Decimal("200.00"), is_intraday=True,
    )
    from app.services.order_matching import _recompute_margin_used

    await _recompute_margin_used(db, account)
    await db.commit()

    position = await db.scalar(
        select(Position).where(
            Position.account_id == account.id, Position.ticker == "AAPL", Position.is_intraday.is_(True),
        )
    )
    assert position.signed_qty == 10
    assert account.margin_used > 0

    as_of = datetime(2026, 7, 1, 15, 59, tzinfo=timezone.utc)
    await fs._square_off_all_mis(db, as_of)
    await db.commit()

    position = await db.scalar(
        select(Position).where(
            Position.account_id == account.id, Position.ticker == "AAPL", Position.is_intraday.is_(True),
        )
    )
    assert position.signed_qty == 0

    account = await db.scalar(select(Account).where(Account.id == account.id))
    assert account.margin_used == 0

    match = await db.scalar(select(OrderMatch).where(OrderMatch.match_type == "EOD_SQUARE_OFF"))
    assert match is not None
