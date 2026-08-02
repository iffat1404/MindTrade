"""Unit tests for behavioral_guard.py: the 7 deterministic bias detectors,
BRS risk bands, rolling trader-behavioral-history tracking (streaks,
last-loss, day rollover, recent trades), and the weekly profile aggregation.

Each bias-detector test monkeypatches the specific data-lookup helper(s) it
needs (price change, sentiment, headline count, position) rather than
seeding real price/news rows -- this isolates exactly the signal under test
from the ~120k rows of real seeded market data already in the dev DB, and
from any FeedState left over from other test files.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.security import get_password_hash
from app.models.orm import Account, BehavioralScore
from app.models.schemas import OrderCreateRequest
from app.services import behavioral_guard as bg

pytestmark = pytest.mark.asyncio(loop_scope="session")

MARKET_TIME = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


async def _make_account(db) -> Account:
    account = Account(
        username=f"pytest_bg_{uuid.uuid4().hex[:12]}",
        password_hash=get_password_hash("irrelevant"),
        role="trader",
        kyc_status="APPROVED",
        cash_balance=Decimal("1000000.00"),
        starting_capital=Decimal("1000000.00"),
    )
    db.add(account)
    await db.flush()
    return account


def _order(**kw) -> OrderCreateRequest:
    defaults = dict(ticker="ZZZZ", side="BUY", order_type="MARKET", qty=10, product_type="CNC")
    defaults.update(kw)
    return OrderCreateRequest(**defaults)


def _patch_neutral_lookups(monkeypatch) -> None:
    """No signal should fire from these unless a test explicitly overrides
    one -- the safe/neutral defaults for every non-history-based check.
    """
    async def _no_price_change(db, ticker, bars, market_time):
        return None

    async def _no_sentiment(db, ticker, as_of):
        return None

    async def _no_headlines(db, ticker, as_of):
        return 0

    async def _no_position(db, account_id, ticker, is_intraday):
        return None

    monkeypatch.setattr(bg, "_price_change_pct", _no_price_change)
    monkeypatch.setattr(bg, "_avg_sentiment", _no_sentiment)
    monkeypatch.setattr(bg, "_headline_count", _no_headlines)
    monkeypatch.setattr(bg, "_get_position", _no_position)


# ---------------------------------------------------------------------------
# risk_level bands
# ---------------------------------------------------------------------------
async def test_risk_level_bands():
    assert bg.risk_level(0) == "CLEAR"
    assert bg.risk_level(25) == "CLEAR"
    assert bg.risk_level(26) == "CAUTION"
    assert bg.risk_level(50) == "CAUTION"
    assert bg.risk_level(51) == "WARNING"
    assert bg.risk_level(75) == "WARNING"
    assert bg.risk_level(76) == "HIGH_RISK"
    assert bg.risk_level(100) == "HIGH_RISK"


# ---------------------------------------------------------------------------
# calculate_brs -- one test per bias detector, isolated via monkeypatching
# ---------------------------------------------------------------------------
async def test_calculate_brs_clear_when_no_signals(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)
    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(), history=history, market_time=MARKET_TIME
    )
    assert report.brs == 0
    assert report.signals == []


async def test_calculate_brs_revenge_trading(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)
    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)
    history.last_loss_at = MARKET_TIME - timedelta(minutes=2)
    history.avg_order_size = Decimal("1000.00")

    order = _order(order_type="LIMIT", qty=100, limit_price=Decimal("50.00"))  # notional 5000 = 5x avg
    report = await bg.calculate_brs(
        db, account_id=account.id, order=order, history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "REVENGE_TRADING")
    assert signal.score == 30
    assert report.brs == 30


async def test_calculate_brs_revenge_trading_not_triggered_after_5_minutes(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)
    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)
    history.last_loss_at = MARKET_TIME - timedelta(minutes=10)
    history.avg_order_size = Decimal("1000.00")

    order = _order(order_type="LIMIT", qty=100, limit_price=Decimal("50.00"))
    report = await bg.calculate_brs(
        db, account_id=account.id, order=order, history=history, market_time=MARKET_TIME
    )
    assert report.brs == 0


async def test_calculate_brs_overconfidence(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)
    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)
    history.streak_type = "WIN"
    history.streak_count = 5
    history.today_order_count = 6

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(), history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "OVERCONFIDENCE")
    assert signal.score == 20


async def test_calculate_brs_fomo(db, monkeypatch):
    async def _price_change(db, ticker, bars, market_time):
        return Decimal("0.10") if bars == 5 else None

    async def _sentiment(db, ticker, as_of):
        return Decimal("0.8")

    monkeypatch.setattr(bg, "_price_change_pct", _price_change)
    monkeypatch.setattr(bg, "_avg_sentiment", _sentiment)
    monkeypatch.setattr(bg, "_headline_count", lambda *a, **k: _async_return(0))
    monkeypatch.setattr(bg, "_get_position", lambda *a, **k: _async_return(None))

    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(side="BUY"), history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "FOMO")
    assert signal.score == 16  # min(20, int(20 * 0.8))


async def test_calculate_brs_loss_aversion(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)

    async def _fake_position(db, account_id, ticker, is_intraday):
        return types.SimpleNamespace(signed_qty=10, avg_cost=Decimal("100.00"))

    async def _fake_last_price(db, ticker):
        return Decimal("90.00")  # 10% below avg_cost

    monkeypatch.setattr(bg, "_get_position", _fake_position)
    monkeypatch.setattr(bg, "get_last_price", _fake_last_price)

    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(side="BUY"), history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "LOSS_AVERSION")
    assert signal.score == 15  # min(15, int(15 * 0.10 * 10))


async def test_calculate_brs_recency_bias(db, monkeypatch):
    async def _price_change(db, ticker, bars, market_time):
        return Decimal("0.05") if bars == 3 else None

    monkeypatch.setattr(bg, "_price_change_pct", _price_change)
    monkeypatch.setattr(bg, "_avg_sentiment", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(bg, "_headline_count", lambda *a, **k: _async_return(0))
    monkeypatch.setattr(bg, "_get_position", lambda *a, **k: _async_return(None))

    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(side="BUY"), history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "RECENCY_BIAS")
    assert signal.score == 10


async def test_calculate_brs_herding(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)

    async def _headlines(db, ticker, as_of):
        return 15

    async def _sentiment(db, ticker, as_of):
        return Decimal("0.7")

    monkeypatch.setattr(bg, "_headline_count", _headlines)
    monkeypatch.setattr(bg, "_avg_sentiment", _sentiment)

    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(side="BUY"), history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "HERDING")
    assert signal.score == 5


async def test_calculate_brs_overtrading(db, monkeypatch):
    _patch_neutral_lookups(monkeypatch)
    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)
    history.today_order_count = 15

    report = await bg.calculate_brs(
        db, account_id=account.id, order=_order(), history=history, market_time=MARKET_TIME
    )
    signal = next(s for s in report.signals if s.type == "OVERTRADING")
    assert signal.score == 7  # min(10, 15 - 8)


async def test_calculate_brs_caps_at_100(db, monkeypatch):
    async def _price_change(db, ticker, bars, market_time):
        return Decimal("0.10")  # >0.05 for FOMO, >0.03 for recency

    async def _sentiment(db, ticker, as_of):
        return Decimal("1.0")

    async def _headlines(db, ticker, as_of):
        return 20

    async def _position(db, account_id, ticker, is_intraday):
        return types.SimpleNamespace(signed_qty=10, avg_cost=Decimal("100.00"))

    async def _last_price(db, ticker):
        return Decimal("50.00")  # 50% below avg_cost

    monkeypatch.setattr(bg, "_price_change_pct", _price_change)
    monkeypatch.setattr(bg, "_avg_sentiment", _sentiment)
    monkeypatch.setattr(bg, "_headline_count", _headlines)
    monkeypatch.setattr(bg, "_get_position", _position)
    monkeypatch.setattr(bg, "get_last_price", _last_price)

    account = await _make_account(db)
    history = await bg.get_or_create_history(db, account.id)
    history.last_loss_at = MARKET_TIME - timedelta(minutes=1)
    history.avg_order_size = Decimal("100.00")
    history.streak_type = "WIN"
    history.streak_count = 10
    history.today_order_count = 20

    order = _order(side="BUY", order_type="LIMIT", qty=100, limit_price=Decimal("50.00"))
    report = await bg.calculate_brs(
        db, account_id=account.id, order=order, history=history, market_time=MARKET_TIME
    )
    assert report.brs == 100


async def _async_return(value):
    return value


# ---------------------------------------------------------------------------
# Rolling history: order placement, fill outcomes, day rollover
# ---------------------------------------------------------------------------
async def test_record_order_placed_increments_and_rolls_over_new_day(db):
    account = await _make_account(db)

    await bg.record_order_placed(db, account_id=account.id, market_time=MARKET_TIME)
    await bg.record_order_placed(db, account_id=account.id, market_time=MARKET_TIME + timedelta(minutes=5))
    history = await bg.get_or_create_history(db, account.id)
    assert history.today_order_count == 2

    next_day = MARKET_TIME + timedelta(days=1)
    await bg.record_order_placed(db, account_id=account.id, market_time=next_day)
    history = await bg.get_or_create_history(db, account.id)
    assert history.today_order_count == 1  # rolled over, not 3
    assert history.session_start == next_day


async def test_record_fill_outcome_tracks_loss_then_win_streak(db):
    account = await _make_account(db)

    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="SELL", qty=10, price=Decimal("90.00"),
        realized_pnl=Decimal("-100.00"), market_time=MARKET_TIME,
    )
    history = await bg.get_or_create_history(db, account.id)
    assert history.streak_type == "LOSS"
    assert history.streak_count == 1
    assert history.last_loss_at == MARKET_TIME
    assert history.last_loss_amount == Decimal("-100.00")
    assert history.today_pnl == Decimal("-100.00")

    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="SELL", qty=10, price=Decimal("110.00"),
        realized_pnl=Decimal("-50.00"), market_time=MARKET_TIME + timedelta(minutes=1),
    )
    history = await bg.get_or_create_history(db, account.id)
    assert history.streak_type == "LOSS"
    assert history.streak_count == 2  # consecutive losses accumulate

    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="BUY", qty=10, price=Decimal("100.00"),
        realized_pnl=Decimal("200.00"), market_time=MARKET_TIME + timedelta(minutes=2),
    )
    history = await bg.get_or_create_history(db, account.id)
    assert history.streak_type == "WIN"
    assert history.streak_count == 1  # streak reset on a win after losses
    assert history.today_pnl == Decimal("50.00")  # -100 - 50 + 200


async def test_record_fill_outcome_zero_realized_pnl_does_not_affect_streak(db):
    account = await _make_account(db)
    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="BUY", qty=10, price=Decimal("100.00"),
        realized_pnl=Decimal("0"), market_time=MARKET_TIME,
    )
    history = await bg.get_or_create_history(db, account.id)
    assert history.streak_type is None
    assert history.streak_count == 0


async def test_record_fill_outcome_avg_order_size_ema(db):
    account = await _make_account(db)
    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="BUY", qty=10, price=Decimal("100.00"),
        realized_pnl=Decimal("0"), market_time=MARKET_TIME,
    )
    history = await bg.get_or_create_history(db, account.id)
    assert history.avg_order_size == Decimal("1000.00")  # first fill sets it directly

    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="BUY", qty=10, price=Decimal("200.00"),
        realized_pnl=Decimal("0"), market_time=MARKET_TIME + timedelta(minutes=1),
    )
    history = await bg.get_or_create_history(db, account.id)
    # EMA: 1000 * 0.8 + 2000 * 0.2 = 1200
    assert history.avg_order_size == Decimal("1200.00")


async def test_record_fill_outcome_recent_trades_capped_at_20(db):
    account = await _make_account(db)
    for i in range(25):
        await bg.record_fill_outcome(
            db, account_id=account.id, ticker="ZZZZ", side="BUY", qty=1, price=Decimal("100.00"),
            realized_pnl=Decimal("0"), market_time=MARKET_TIME + timedelta(minutes=i),
        )
    history = await bg.get_or_create_history(db, account.id)
    assert len(history.recent_trades) == 20
    # oldest trades (minutes 0-4) should have been dropped, newest kept
    assert history.recent_trades[0]["timestamp"] == (MARKET_TIME + timedelta(minutes=5)).isoformat()
    assert history.recent_trades[-1]["timestamp"] == (MARKET_TIME + timedelta(minutes=24)).isoformat()


# ---------------------------------------------------------------------------
# build_intervention_context
# ---------------------------------------------------------------------------
async def test_build_intervention_context(db):
    account = await _make_account(db)
    await bg.record_fill_outcome(
        db, account_id=account.id, ticker="ZZZZ", side="BUY", qty=1, price=Decimal("100.00"),
        realized_pnl=Decimal("50.00"), market_time=MARKET_TIME,
    )
    history = await bg.get_or_create_history(db, account.id)
    context = bg.build_intervention_context(history, MARKET_TIME + timedelta(minutes=3))
    assert context["minutes_since_last_trade"] == 3.0
    assert context["today_pnl"] == "50.00"
    assert len(context["recent_trades"]) == 1


# ---------------------------------------------------------------------------
# get_weekly_profile
# ---------------------------------------------------------------------------
async def test_get_weekly_profile_aggregates_trend_biases_and_interventions(db):
    account = await _make_account(db)

    def _score(brs, biases, calculated_at, *, shown=False, proceeded=None):
        return BehavioralScore(
            account_id=account.id, brs=brs, biases_detected=biases,
            intervention_shown=shown, trader_proceeded=proceeded, calculated_at=calculated_at,
        )

    db.add_all(
        [
            _score(30, [{"type": "REVENGE_TRADING"}], MARKET_TIME - timedelta(days=1), shown=True, proceeded=True),
            _score(40, [{"type": "REVENGE_TRADING"}], MARKET_TIME - timedelta(days=1), shown=True, proceeded=False),
            _score(60, [{"type": "FOMO"}], MARKET_TIME - timedelta(days=2), shown=True, proceeded=None),
            _score(10, [], MARKET_TIME - timedelta(days=3)),
        ]
    )
    await db.flush()

    profile = await bg.get_weekly_profile(db, account.id, MARKET_TIME)

    assert len(profile.brs_trend) == 3  # 3 distinct days with scores
    top_types = [b.type for b in profile.top_biases]
    assert top_types[0] == "REVENGE_TRADING"  # 2 occurrences, most common

    assert profile.intervention_summary.shown == 3
    assert profile.intervention_summary.proceeded == 1
    assert profile.intervention_summary.cancelled == 1
    assert profile.intervention_summary.pending == 1


async def test_get_weekly_profile_excludes_scores_outside_window(db):
    account = await _make_account(db)
    db.add(
        BehavioralScore(
            account_id=account.id, brs=90, biases_detected=[], calculated_at=MARKET_TIME - timedelta(days=30)
        )
    )
    await db.flush()

    profile = await bg.get_weekly_profile(db, account.id, MARKET_TIME)
    assert profile.brs_trend == []
    assert profile.intervention_summary.shown == 0
