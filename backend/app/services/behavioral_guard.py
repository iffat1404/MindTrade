import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import (
    Account,
    BehavioralScore,
    KYCSubmission,
    NewsSentimentDaily,
    Position,
    PriceHistoryMinute,
    TraderBehavioralHistory,
)
from app.models.schemas import OrderCreateRequest
from app.services.order_engine import get_last_price

RECENT_TRADES_LIMIT = 20
_AVG_ORDER_SIZE_EMA_ALPHA = Decimal("0.2")  # see record_fill_outcome for why this is an EMA, not a true mean


@dataclass
class BiasSignal:
    type: str
    score: int
    detail: str


@dataclass
class BiasReport:
    brs: int
    signals: list[BiasSignal] = field(default_factory=list)


@dataclass
class BRSTrendPoint:
    date: date
    avg_brs: float


@dataclass
class TopBiasEntry:
    type: str
    count: int


@dataclass
class InterventionSummary:
    shown: int
    proceeded: int
    cancelled: int
    pending: int


@dataclass
class BehavioralProfile:
    brs_trend: list[BRSTrendPoint]
    top_biases: list[TopBiasEntry]
    intervention_summary: InterventionSummary
    best_trading_window: Optional[str]


def risk_level(brs: int) -> str:
    if brs <= settings.BRS_CLEAR_MAX:
        return "CLEAR"
    if brs <= settings.BRS_CAUTION_MAX:
        return "CAUTION"
    if brs <= settings.BRS_WARNING_MAX:
        return "WARNING"
    return "HIGH_RISK"


# ---------------------------------------------------------------------------
# Trader behavioral history (rolling state used to detect future biases)
# ---------------------------------------------------------------------------
async def get_or_create_history(db: AsyncSession, account_id: uuid.UUID) -> TraderBehavioralHistory:
    history = await db.get(TraderBehavioralHistory, account_id)
    if history is None:
        history = TraderBehavioralHistory(account_id=account_id)
        db.add(history)
        await db.flush()
    return history


def _roll_day_if_needed(history: TraderBehavioralHistory, market_time: datetime) -> None:
    """Resets the "today" counters when `market_time` has moved to a new
    simulated day since the last update -- session_start doubles as "the
    last day these counters were valid for" rather than a separate flag.
    """
    if history.session_start is None or history.session_start.date() != market_time.date():
        history.session_start = market_time
        history.today_order_count = 0
        history.today_pnl = Decimal("0")


async def record_order_placed(db: AsyncSession, *, account_id: uuid.UUID, market_time: datetime) -> None:
    """Called when an order actually reaches ROUTED (passed validation) --
    today_order_count/streak checks are about real trading activity, not
    rejected attempts.
    """
    history = await get_or_create_history(db, account_id)
    _roll_day_if_needed(history, market_time)
    history.today_order_count += 1
    history.updated_at = market_time


async def record_fill_outcome(
    db: AsyncSession,
    *,
    account_id: uuid.UUID,
    ticker: str,
    side: str,
    qty: int,
    price: Decimal,
    realized_pnl: Decimal,
    market_time: datetime,
) -> None:
    """Updates rolling behavioral state after a fill settles. Called from
    order_matching._settle_fill for every fill (including EOD square-offs).
    """
    history = await get_or_create_history(db, account_id)
    _roll_day_if_needed(history, market_time)

    notional = qty * price
    # avg_order_size is an exponential moving average, not a true lifetime
    # mean -- trader_behavioral_history has no running-count column to
    # divide by, and an EMA gives a reasonable "typical size" baseline for
    # the revenge-trading size-ratio check without needing one.
    if history.avg_order_size == 0:
        history.avg_order_size = notional
    else:
        history.avg_order_size = (
            history.avg_order_size * (1 - _AVG_ORDER_SIZE_EMA_ALPHA) + notional * _AVG_ORDER_SIZE_EMA_ALPHA
        )

    history.today_pnl += realized_pnl

    if realized_pnl < 0:
        history.last_loss_at = market_time
        history.last_loss_amount = realized_pnl
        history.streak_count = history.streak_count + 1 if history.streak_type == "LOSS" else 1
        history.streak_type = "LOSS"
    elif realized_pnl > 0:
        history.streak_count = history.streak_count + 1 if history.streak_type == "WIN" else 1
        history.streak_type = "WIN"
    # realized_pnl == 0 (position opened/extended, nothing closed yet) doesn't
    # change the streak -- there's no win/loss outcome to speak of.

    trades = list(history.recent_trades or [])
    trades.append(
        {
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": str(price),
            "realized_pnl": str(realized_pnl),
            "timestamp": market_time.isoformat(),
        }
    )
    history.recent_trades = trades[-RECENT_TRADES_LIMIT:]
    history.updated_at = market_time


def build_intervention_context(history: TraderBehavioralHistory, market_time: datetime) -> dict:
    """Assembles the plain-data "trading context" passed to
    genai_client.generate_behavioral_intervention -- kept a plain dict (not
    the ORM row) so genai_client.py stays DB-agnostic, same as
    extract_kyc_fields taking raw bytes rather than a KYCSubmission.
    """
    recent_trades = history.recent_trades or []
    minutes_since_last_trade = None
    if recent_trades:
        last_ts = datetime.fromisoformat(recent_trades[-1]["timestamp"])
        minutes_since_last_trade = round((market_time - last_ts).total_seconds() / 60, 1)
    return {
        "recent_trades": recent_trades[-5:],
        "today_pnl": str(history.today_pnl),
        "minutes_since_last_trade": minutes_since_last_trade,
    }


# ---------------------------------------------------------------------------
# Bias detection helpers (data lookups)
# ---------------------------------------------------------------------------
async def _price_change_pct(db: AsyncSession, ticker: str, bars: int, market_time: datetime) -> Optional[Decimal]:
    closes = (
        await db.scalars(
            select(PriceHistoryMinute.close)
            .where(PriceHistoryMinute.ticker == ticker, PriceHistoryMinute.timestamp <= market_time)
            .order_by(PriceHistoryMinute.timestamp.desc())
            .limit(bars + 1)
        )
    ).all()
    if len(closes) < 2:
        return None
    latest, oldest = closes[0], closes[-1]
    if oldest == 0:
        return None
    return (latest - oldest) / oldest


async def _avg_sentiment(db: AsyncSession, ticker: str, as_of: date) -> Optional[Decimal]:
    return await db.scalar(
        select(NewsSentimentDaily.avg_sentiment).where(
            NewsSentimentDaily.ticker == ticker, NewsSentimentDaily.date == as_of
        )
    )


async def _headline_count(db: AsyncSession, ticker: str, as_of: date) -> int:
    count = await db.scalar(
        select(NewsSentimentDaily.headline_count).where(
            NewsSentimentDaily.ticker == ticker, NewsSentimentDaily.date == as_of
        )
    )
    return count or 0


async def _get_position(db: AsyncSession, account_id: uuid.UUID, ticker: str, is_intraday: bool) -> Optional[Position]:
    return await db.scalar(
        select(Position).where(
            Position.account_id == account_id,
            Position.ticker == ticker,
            Position.is_backtest.is_(False),
            Position.is_intraday.is_(is_intraday),
        )
    )


# ---------------------------------------------------------------------------
# BRS calculation -- 7 deterministic bias detectors (MASTER_BUILD_PLAN
# Part 2 / Sprint 5 task 1's code sketch). Section 2.1 lists an 8th bias
# ("Anchoring") and section 2.2's weight table omits Overconfidence while
# the sketch includes it -- another of this project's recurring
# doc/reality mismatches. Following the concrete 7-check code sketch
# (Revenge/Overconfidence/FOMO/Loss Aversion/Recency/Herding/Overtrading)
# since it's the only fully-specified version, same as prior sprints
# preferred worked examples over prose summaries when the two disagreed.
# ---------------------------------------------------------------------------
async def calculate_brs(
    db: AsyncSession,
    *,
    account_id: uuid.UUID,
    order: OrderCreateRequest,
    history: TraderBehavioralHistory,
    market_time: datetime,
) -> BiasReport:
    signals: list[BiasSignal] = []

    order_price = order.limit_price or await get_last_price(db, order.ticker) or Decimal("0")
    order_notional = order.qty * order_price

    # 1. Revenge Trading (0-30): larger-than-usual order within 5 min of a loss.
    if history.last_loss_at is not None and history.avg_order_size > 0:
        minutes_since_loss = (market_time - history.last_loss_at).total_seconds() / 60
        if minutes_since_loss < 5:
            size_ratio = order_notional / history.avg_order_size
            if size_ratio > Decimal("1.5"):
                score = min(30, int(30 * (size_ratio - 1)))
                signals.append(
                    BiasSignal(
                        "REVENGE_TRADING", score,
                        f"Order placed {minutes_since_loss:.1f}min after a loss, {size_ratio:.1f}x avg size",
                    )
                )

    # 2. Overconfidence (0-20): win streak + high order frequency today.
    if history.streak_type == "WIN" and history.streak_count >= 3 and history.today_order_count >= 5:
        score = min(20, 10 + history.streak_count * 2)
        signals.append(
            BiasSignal(
                "OVERCONFIDENCE", score,
                f"{history.streak_count}-trade win streak + {history.today_order_count} orders today",
            )
        )

    # 3. FOMO (0-20): buying after a sharp recent move, with high sentiment.
    change_5bar = await _price_change_pct(db, order.ticker, 5, market_time)
    if order.side == "BUY" and change_5bar is not None and change_5bar > Decimal("0.05"):
        sentiment = await _avg_sentiment(db, order.ticker, market_time.date())
        if sentiment is not None and sentiment > Decimal("0.5"):
            score = min(20, int(20 * sentiment))
            signals.append(
                BiasSignal("FOMO", score, f"Buying after +{change_5bar:.1%} move with sentiment {sentiment:.2f}")
            )

    # 4. Loss Aversion / Averaging Down (0-15): buying more of a losing long.
    position = await _get_position(db, account_id, order.ticker, order.product_type == "MIS")
    if position is not None and position.signed_qty > 0 and order.side == "BUY" and position.avg_cost > 0:
        last_price = await get_last_price(db, order.ticker)
        if last_price is not None:
            unrealized_pct = (position.avg_cost - last_price) / position.avg_cost
            if unrealized_pct > Decimal("0.05"):
                score = min(15, int(15 * unrealized_pct * 10))
                signals.append(
                    BiasSignal(
                        "LOSS_AVERSION", score,
                        f"Averaging down on {order.ticker}, which is {unrealized_pct:.1%} in loss",
                    )
                )

    # 5. Recency Bias (0-10): order direction chases the last 3 bars' momentum.
    change_3bar = await _price_change_pct(db, order.ticker, 3, market_time)
    if change_3bar is not None and abs(change_3bar) > Decimal("0.03"):
        momentum_direction = "BUY" if change_3bar > 0 else "SELL"
        if order.side == momentum_direction:
            signals.append(BiasSignal("RECENCY_BIAS", 10, "Order direction matches last 3 bars' momentum"))

    # 6. Herding (0-5): buying into a wave of same-day bullish headlines.
    headlines = await _headline_count(db, order.ticker, market_time.date())
    sentiment_today = await _avg_sentiment(db, order.ticker, market_time.date())
    if headlines > 10 and sentiment_today is not None and sentiment_today > Decimal("0.6") and order.side == "BUY":
        signals.append(
            BiasSignal("HERDING", 5, f"{headlines} bullish articles today -- crowd may be piling in")
        )

    # 7. Overtrading (0-10): more than 8 orders already placed this session.
    if history.today_order_count > 8:
        score = min(10, history.today_order_count - 8)
        signals.append(BiasSignal("OVERTRADING", score, f"{history.today_order_count} orders placed today"))

    total = min(100, sum(s.score for s in signals))
    return BiasReport(brs=total, signals=signals)


# ---------------------------------------------------------------------------
# GET /api/trader/behavioral-profile -- weekly dashboard data
# ---------------------------------------------------------------------------
async def get_weekly_profile(
    db: AsyncSession, account_id: uuid.UUID, market_time: Optional[datetime]
) -> BehavioralProfile:
    if market_time is None:
        market_time = datetime.now(timezone.utc)
    window_start = market_time - timedelta(days=7)

    scores = (
        await db.scalars(
            select(BehavioralScore).where(
                BehavioralScore.account_id == account_id,
                BehavioralScore.calculated_at >= window_start,
                BehavioralScore.calculated_at <= market_time,
            )
        )
    ).all()

    by_day: dict[date, list[int]] = {}
    bias_counts: dict[str, int] = {}
    shown = proceeded = cancelled = pending = 0
    bucket_scores: dict[tuple[str, int], list[int]] = {}

    for s in scores:
        by_day.setdefault(s.calculated_at.date(), []).append(s.brs)

        for bias in s.biases_detected or []:
            bias_type = bias.get("type")
            if bias_type:
                bias_counts[bias_type] = bias_counts.get(bias_type, 0) + 1

        if s.intervention_shown:
            shown += 1
            if s.trader_proceeded is True:
                proceeded += 1
            elif s.trader_proceeded is False:
                cancelled += 1
            else:
                pending += 1

        bucket_key = (s.calculated_at.strftime("%A"), (s.calculated_at.hour // 2) * 2)
        bucket_scores.setdefault(bucket_key, []).append(s.brs)

    brs_trend = [
        BRSTrendPoint(date=day, avg_brs=sum(vals) / len(vals)) for day, vals in sorted(by_day.items())
    ]
    top_biases = [
        TopBiasEntry(type=t, count=c) for t, c in sorted(bias_counts.items(), key=lambda kv: -kv[1])[:3]
    ]

    best_trading_window = None
    min_samples = 3
    eligible = {k: v for k, v in bucket_scores.items() if len(v) >= min_samples}
    if eligible:
        best_key = min(eligible, key=lambda k: sum(eligible[k]) / len(eligible[k]))
        weekday, hour = best_key
        best_trading_window = f"{weekday} {hour:02d}:00-{(hour + 2) % 24:02d}:00"

    return BehavioralProfile(
        brs_trend=brs_trend,
        top_biases=top_biases,
        intervention_summary=InterventionSummary(shown=shown, proceeded=proceeded, cancelled=cancelled, pending=pending),
        best_trading_window=best_trading_window,
    )


# ---------------------------------------------------------------------------
# Admin behavioral analytics (Part 2.7)
# ---------------------------------------------------------------------------
def _age_group(dob: date, as_of: date) -> str:
    age = as_of.year - dob.year - ((as_of.month, as_of.day) < (dob.month, dob.day))
    if age < 26:
        return "18-25"
    if age < 36:
        return "26-35"
    if age < 51:
        return "36-50"
    return "51+"


async def get_platform_analytics(db: AsyncSession) -> dict:
    """Platform-wide behavioral stats. Does NOT include the "high-BRS
    trades -> loss rate" correlation Part 2.7 also asks for: per-fill
    realized P&L is never persisted (only Position.realized_pnl, an
    account+ticker aggregate), so there's no reliable way to attribute a
    specific historical order to a win/loss outcome after the fact without
    adding that instrumentation -- out of scope for Sprint 5.
    """
    scores = (await db.scalars(select(BehavioralScore))).all()

    distribution = {"CLEAR": 0, "CAUTION": 0, "WARNING": 0, "HIGH_RISK": 0}
    shown = proceeded = 0
    for s in scores:
        distribution[risk_level(s.brs)] += 1
        if s.intervention_shown:
            shown += 1
            if s.trader_proceeded is True:
                proceeded += 1

    dob_by_account = dict(
        (
            await db.execute(
                select(KYCSubmission.account_id, KYCSubmission.extracted_dob).where(
                    KYCSubmission.status == "APPROVED", KYCSubmission.extracted_dob.is_not(None)
                )
            )
        ).all()
    )

    today = datetime.now(timezone.utc).date()
    bias_by_age_group: dict[str, dict[str, int]] = {}
    for s in scores:
        dob = dob_by_account.get(s.account_id)
        if dob is None:
            continue
        group = _age_group(dob, today)
        group_counts = bias_by_age_group.setdefault(group, {})
        for bias in s.biases_detected or []:
            bias_type = bias.get("type")
            if bias_type:
                group_counts[bias_type] = group_counts.get(bias_type, 0) + 1

    most_common_bias_by_age_group = [
        {"age_group": group, "top_bias": max(counts, key=counts.get), "count": max(counts.values())}
        for group, counts in bias_by_age_group.items()
        if counts
    ]

    return {
        "total_scores_calculated": len(scores),
        "brs_distribution": distribution,
        "intervention_acceptance_rate": (proceeded / shown) if shown else None,
        "most_common_bias_by_age_group": most_common_bias_by_age_group,
    }


async def get_account_analytics(db: AsyncSession, account: Account) -> dict:
    scores = (
        await db.scalars(
            select(BehavioralScore)
            .where(BehavioralScore.account_id == account.id)
            .order_by(BehavioralScore.calculated_at.desc())
            .limit(50)
        )
    ).all()

    bias_counts: dict[str, int] = {}
    for s in scores:
        for bias in s.biases_detected or []:
            bias_type = bias.get("type")
            if bias_type:
                bias_counts[bias_type] = bias_counts.get(bias_type, 0) + 1

    avg_brs = sum(s.brs for s in scores) / len(scores) if scores else None

    return {
        "account_id": account.id,
        "username": account.username,
        "scores_analyzed": len(scores),
        "avg_brs": avg_brs,
        "bias_fingerprint": sorted(
            ({"type": t, "count": c} for t, c in bias_counts.items()), key=lambda d: -d["count"]
        ),
        "consistently_high_brs": avg_brs is not None and avg_brs > settings.BRS_WARNING_MAX,
    }
