import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import BehavioralScore, Fill, Order
from app.services import behavioral_guard

# Task 6.1's 3 critical-moment kinds (MASTER_BUILD_PLAN Sprint 6).
KIND_HIGHEST_BRS = "highest_brs"
KIND_GUARDRAIL_FIRED = "guardrail_fired"
KIND_PATTERN_REPEAT = "pattern_repeat"


@dataclass
class CriticalMoment:
    kind: str
    display_type: str  # "nemesis" | "caution" | "success" -- the frontend's color-coded badge
    behavioral_score_id: uuid.UUID
    order_id: uuid.UUID
    timestamp: datetime
    ticker: str
    side: str
    bias_name: Optional[str]
    brs: int
    intervention_shown: bool
    trader_proceeded: Optional[bool]
    realized_pnl: Optional[Decimal]


@dataclass
class SessionStats:
    brs: Optional[float]
    trade_count: int
    win_rate: Optional[float]
    worst_pattern: Optional[str]
    best_window: Optional[str]


@dataclass
class SessionReview:
    date: date_type
    critical_moments: list[CriticalMoment] = field(default_factory=list)
    session_stats: Optional[SessionStats] = None


def _display_type_for(score: BehavioralScore, *, is_repeat: bool) -> str:
    """Maps a BehavioralScore's outcome to the 3-color badge system
    (nemesis/caution/success) FRONTEND_DESIGN_GUIDE's Session Review page
    uses. Not specified anywhere in the docs beyond the 3 label names, so
    this is a judgment call: a recurring bias is always the "nemesis"
    regardless of outcome (it's the moment meant to feel like a demon
    pattern), a shown-and-ignored intervention is a "caution" (the fork in
    the road), a shown-and-heeded intervention is a "success" (discipline
    rewarded), and a high-BRS moment with no intervention shown defaults
    to "success" (nothing to warn about, still worth noting).
    """
    if is_repeat:
        return "nemesis"
    if score.intervention_shown:
        return "success" if score.trader_proceeded is False else "caution"
    return "success"


def _bias_names(score: BehavioralScore) -> list[str]:
    return [b.get("type") for b in (score.biases_detected or []) if b.get("type")]


def _top_bias_name(score: BehavioralScore) -> Optional[str]:
    biases = score.biases_detected or []
    if not biases:
        return None
    return max(biases, key=lambda b: b.get("score", 0)).get("type")


async def _moment_from_score(db: AsyncSession, score: BehavioralScore, kind: str, *, is_repeat: bool) -> Optional[CriticalMoment]:
    if score.order_id is None:
        return None
    order = await db.get(Order, score.order_id)
    if order is None:
        return None

    fill_row = await db.scalar(
        select(Fill).where(Fill.order_id == order.id).order_by(Fill.timestamp.desc()).limit(1)
    )
    realized_pnl = fill_row.realized_pnl if fill_row is not None else None

    return CriticalMoment(
        kind=kind,
        display_type=_display_type_for(score, is_repeat=is_repeat),
        behavioral_score_id=score.id,
        order_id=order.id,
        timestamp=score.calculated_at,
        ticker=order.ticker,
        side=order.side,
        bias_name=_top_bias_name(score),
        brs=score.brs,
        intervention_shown=score.intervention_shown,
        trader_proceeded=score.trader_proceeded,
        realized_pnl=realized_pnl,
    )


async def analyze_session(db: AsyncSession, account_id: uuid.UUID, session_date: date_type) -> SessionReview:
    """Identifies the day's 3 critical moments (Task 6.1): the highest-BRS
    trade, the moment the guardrail fired, and a recurring bias pattern --
    plus session-level stats. Only considers BehavioralScore rows linked to
    a real order (order_id set), since a "critical moment" needs an actual
    trade and outcome to analyze, not just a check that was never acted on.
    """
    day_start = datetime.combine(session_date, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(session_date, time.max, tzinfo=timezone.utc)

    scores = (
        await db.scalars(
            select(BehavioralScore)
            .where(
                BehavioralScore.account_id == account_id,
                BehavioralScore.order_id.is_not(None),
                BehavioralScore.calculated_at >= day_start,
                BehavioralScore.calculated_at <= day_end,
            )
            .order_by(BehavioralScore.brs.desc())
        )
    ).all()

    fills = (
        await db.scalars(
            select(Fill)
            .join(Order, Fill.order_id == Order.id)
            .where(Order.account_id == account_id, Fill.timestamp >= day_start, Fill.timestamp <= day_end)
        )
    ).all()

    trade_count = len(fills)
    decisive_fills = [f for f in fills if f.realized_pnl is not None and f.realized_pnl != 0]
    win_rate = (
        round(100 * sum(1 for f in decisive_fills if f.realized_pnl > 0) / len(decisive_fills), 1)
        if decisive_fills
        else None
    )
    avg_brs = round(sum(s.brs for s in scores) / len(scores), 1) if scores else None

    weekly = await behavioral_guard.get_weekly_profile(db, account_id, day_end)
    worst_pattern = None
    if weekly.top_biases:
        top = weekly.top_biases[0]
        label = top.type.replace("_", " ").title()
        worst_pattern = f"{label} ({top.count} times this week)"

    moments: list[CriticalMoment] = []
    used_score_ids: set[uuid.UUID] = set()

    if scores:
        highest = scores[0]  # already sorted by brs desc
        moment = await _moment_from_score(db, highest, KIND_HIGHEST_BRS, is_repeat=False)
        if moment is not None:
            moments.append(moment)
            used_score_ids.add(highest.id)

    guardrail_candidates = [s for s in scores if s.intervention_shown and s.id not in used_score_ids]
    if guardrail_candidates:
        candidate = guardrail_candidates[0]  # still brs-desc ordered
        moment = await _moment_from_score(db, candidate, KIND_GUARDRAIL_FIRED, is_repeat=False)
        if moment is not None:
            moments.append(moment)
            used_score_ids.add(candidate.id)

    bias_counts: dict[str, int] = {}
    for s in scores:
        for bias_type in _bias_names(s):
            bias_counts[bias_type] = bias_counts.get(bias_type, 0) + 1
    repeated_biases = {b for b, count in bias_counts.items() if count >= 2}
    if repeated_biases:
        repeat_candidates = [
            s for s in scores if s.id not in used_score_ids and any(b in repeated_biases for b in _bias_names(s))
        ]
        if repeat_candidates:
            candidate = repeat_candidates[0]
            moment = await _moment_from_score(db, candidate, KIND_PATTERN_REPEAT, is_repeat=True)
            if moment is not None:
                moments.append(moment)
                used_score_ids.add(candidate.id)

    stats = SessionStats(
        brs=avg_brs, trade_count=trade_count, win_rate=win_rate,
        worst_pattern=worst_pattern, best_window=weekly.best_trading_window,
    )
    return SessionReview(date=session_date, critical_moments=moments, session_stats=stats)
