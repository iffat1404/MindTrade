"""Unit tests for session_analyzer.analyze_session: selecting the day's 3
critical moments (highest BRS, guardrail fired, recurring pattern) from
BehavioralScore + Fill rows, and the session-level stats (win rate, avg
BRS, worst pattern, best window -- the latter two delegated to
behavioral_guard.get_weekly_profile).
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.security import get_password_hash
from app.models.orm import Account, BehavioralScore, Fill, Order
from app.services import session_analyzer as sa

pytestmark = pytest.mark.asyncio(loop_scope="session")

SESSION_DATE = date(2026, 7, 15)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 15, hour, minute, tzinfo=timezone.utc)


async def _make_account(db) -> Account:
    account = Account(
        username=f"pytest_sa_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status="APPROVED",
        cash_balance=Decimal("1000000.00"),
        starting_capital=Decimal("1000000.00"),
    )
    db.add(account)
    await db.flush()
    return account


async def _make_order(db, account, *, ticker="AAPL", side="BUY", qty=10) -> Order:
    order = Order(
        account_id=account.id, ticker=ticker, side=side, order_type="MARKET",
        product_type="CNC", qty=qty, remaining_qty=0, status="FILLED",
    )
    db.add(order)
    await db.flush()
    return order


async def _make_fill(db, order, *, realized_pnl, price=Decimal("100.00"), qty=10, timestamp) -> Fill:
    fill = Fill(
        order_id=order.id, fill_price=price, fill_qty=qty, fees=Decimal("1.00"),
        reason="SYNTHETIC_ASK", realized_pnl=realized_pnl, timestamp=timestamp,
    )
    db.add(fill)
    await db.flush()
    return fill


async def _make_score(
    db, account, order, *, brs, biases, intervention_shown=False, trader_proceeded=None, calculated_at
) -> BehavioralScore:
    score = BehavioralScore(
        account_id=account.id, order_id=order.id if order else None, brs=brs, biases_detected=biases,
        intervention_shown=intervention_shown, trader_proceeded=trader_proceeded, calculated_at=calculated_at,
    )
    db.add(score)
    await db.flush()
    return score


async def test_analyze_session_empty_day_returns_no_moments(db):
    account = await _make_account(db)
    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    assert review.critical_moments == []
    assert review.session_stats.trade_count == 0
    assert review.session_stats.win_rate is None
    assert review.session_stats.brs is None


async def test_analyze_session_ignores_scores_without_order_id(db):
    account = await _make_account(db)
    await _make_score(db, account, None, brs=90, biases=[], calculated_at=_at(9, 37))
    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    assert review.critical_moments == []


async def test_analyze_session_selects_highest_brs_moment(db):
    account = await _make_account(db)
    order = await _make_order(db, account, ticker="AAPL")
    await _make_score(
        db, account, order, brs=85, biases=[{"type": "FOMO", "score": 20, "detail": "x"}],
        calculated_at=_at(9, 40),
    )

    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    assert len(review.critical_moments) == 1
    moment = review.critical_moments[0]
    assert moment.kind == sa.KIND_HIGHEST_BRS
    assert moment.brs == 85
    assert moment.ticker == "AAPL"


async def test_analyze_session_selects_distinct_highest_brs_and_guardrail_moments(db):
    account = await _make_account(db)
    order_a = await _make_order(db, account, ticker="AAPL")
    order_b = await _make_order(db, account, ticker="MSFT")

    await _make_score(
        db, account, order_a, brs=90, biases=[{"type": "OVERTRADING", "score": 10, "detail": "x"}],
        intervention_shown=False, calculated_at=_at(9, 40),
    )
    await _make_score(
        db, account, order_b, brs=40, biases=[{"type": "FOMO", "score": 15, "detail": "y"}],
        intervention_shown=True, trader_proceeded=True, calculated_at=_at(10, 15),
    )

    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    kinds = {m.kind for m in review.critical_moments}
    assert kinds == {sa.KIND_HIGHEST_BRS, sa.KIND_GUARDRAIL_FIRED}

    highest = next(m for m in review.critical_moments if m.kind == sa.KIND_HIGHEST_BRS)
    guardrail = next(m for m in review.critical_moments if m.kind == sa.KIND_GUARDRAIL_FIRED)
    assert highest.ticker == "AAPL"
    assert guardrail.ticker == "MSFT"
    assert guardrail.display_type == "caution"  # shown + proceeded anyway


async def test_analyze_session_selects_pattern_repeat_moment(db):
    account = await _make_account(db)
    order1 = await _make_order(db, account, ticker="AAPL")
    order2 = await _make_order(db, account, ticker="AAPL")
    order3 = await _make_order(db, account, ticker="AAPL")

    await _make_score(
        db, account, order1, brs=80, biases=[{"type": "REVENGE_TRADING", "score": 30, "detail": "a"}],
        intervention_shown=False, calculated_at=_at(9, 30),
    )
    await _make_score(
        db, account, order2, brs=60, biases=[{"type": "REVENGE_TRADING", "score": 25, "detail": "b"}],
        intervention_shown=True, trader_proceeded=False, calculated_at=_at(10, 0),
    )
    await _make_score(
        db, account, order3, brs=50, biases=[{"type": "REVENGE_TRADING", "score": 20, "detail": "c"}],
        intervention_shown=False, calculated_at=_at(11, 0),
    )

    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    kinds = {m.kind for m in review.critical_moments}
    assert kinds == {sa.KIND_HIGHEST_BRS, sa.KIND_GUARDRAIL_FIRED, sa.KIND_PATTERN_REPEAT}

    repeat = next(m for m in review.critical_moments if m.kind == sa.KIND_PATTERN_REPEAT)
    assert repeat.display_type == "nemesis"
    assert repeat.bias_name == "REVENGE_TRADING"


async def test_analyze_session_win_rate_and_trade_count_from_fills(db):
    account = await _make_account(db)
    order = await _make_order(db, account, ticker="AAPL", qty=40)
    await _make_fill(db, order, realized_pnl=Decimal("100.00"), timestamp=_at(9, 31))
    await _make_fill(db, order, realized_pnl=Decimal("-50.00"), timestamp=_at(9, 32))
    await _make_fill(db, order, realized_pnl=Decimal("0.00"), timestamp=_at(9, 33))  # opening, not decisive
    await _make_fill(db, order, realized_pnl=Decimal("30.00"), timestamp=_at(9, 34))

    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    assert review.session_stats.trade_count == 4
    assert review.session_stats.win_rate == pytest.approx(66.7, abs=0.1)  # 2 wins / 3 decisive fills


async def test_analyze_session_only_considers_fills_within_the_date(db):
    account = await _make_account(db)
    order = await _make_order(db, account, ticker="AAPL")
    await _make_fill(db, order, realized_pnl=Decimal("100.00"), timestamp=_at(9, 31))
    # A fill from a different day shouldn't count toward this session's stats.
    await _make_fill(
        db, order, realized_pnl=Decimal("-999.00"),
        timestamp=datetime(2026, 7, 16, 9, 31, tzinfo=timezone.utc),
    )

    review = await sa.analyze_session(db, account.id, SESSION_DATE)
    assert review.session_stats.trade_count == 1
