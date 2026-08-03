from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account
from app.services import indicators

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Endpoints beyond indicators (prices, news) land in later sprints.


@router.get("/{ticker}/indicators")
async def get_indicators(
    ticker: str,
    timeframe: Literal["1m", "1d"] = Query(default="1d"),
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await indicators.compute_indicators(db, ticker.strip().upper(), timeframe)
