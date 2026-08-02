from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account
from app.models.schemas import BehavioralHistoryResponse, BehavioralProfileResponse
from app.services import behavioral_guard
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
