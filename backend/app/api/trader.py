from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account
from app.models.schemas import (
    BehavioralHistoryResponse,
    BehavioralProfileResponse,
    CriticalMomentResponse,
    SessionReviewResponse,
    SessionStatsResponse,
)
from app.services import behavioral_guard, genai_analyst, session_analyzer
from app.services.order_engine import get_latest_market_time

router = APIRouter(prefix="/api/trader", tags=["trader"])


@router.get("/behavioral-history", response_model=BehavioralHistoryResponse)
async def behavioral_history(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BehavioralHistoryResponse:
    history = await behavioral_guard.get_or_create_history(db, current_user.id)
    await db.commit()
    return BehavioralHistoryResponse.model_validate(history)


@router.get("/behavioral-profile", response_model=BehavioralProfileResponse)
async def behavioral_profile(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BehavioralProfileResponse:
    market_time = await get_latest_market_time(db)
    profile = await behavioral_guard.get_weekly_profile(db, current_user.id, market_time)
    return BehavioralProfileResponse.model_validate(profile)


@router.get("/session-review", response_model=SessionReviewResponse)
async def session_review(
    review_date: date = Query(..., alias="date"),
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionReviewResponse:
    """Task 6.3: after-market-close review of the day's 3 critical
    moments, each with a Claude-generated plain-English analysis + one
    actionable lesson (Task 6.2). Text only -- no audio, no video.
    """
    review = await session_analyzer.analyze_session(db, current_user.id, review_date)

    moment_responses = []
    for i, moment in enumerate(review.critical_moments):
        analysis = await genai_analyst.generate_moment_analysis(moment)
        moment_responses.append(
            CriticalMomentResponse(
                type=moment.display_type,
                title=f"CRITICAL MOMENT #{i + 1}",
                timestamp=moment.timestamp,
                ticker=moment.ticker,
                side=moment.side,
                bias_name=moment.bias_name,
                brs=moment.brs,
                claude_analysis=analysis.analysis,
                lesson=analysis.lesson,
            )
        )

    return SessionReviewResponse(
        date=review.date,
        critical_moments=moment_responses,
        session_stats=SessionStatsResponse.model_validate(review.session_stats),
    )
