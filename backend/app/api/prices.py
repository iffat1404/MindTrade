from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account, PriceHistoryDaily, PriceHistoryMinute

router = APIRouter(prefix="/api/prices", tags=["prices"])


@router.get("/{ticker}/daily")
async def daily_prices(
    ticker: str, current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        await db.scalars(
            select(PriceHistoryDaily)
            .where(PriceHistoryDaily.ticker == ticker.strip().upper())
            .order_by(PriceHistoryDaily.date.asc())
        )
    ).all()
    return [
        {
            "date": r.date, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
        }
        for r in rows
    ]


@router.get("/{ticker}/intraday")
async def intraday_prices(
    ticker: str, current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    # interval=1m is the only granularity the feed loads (see
    # services/data/loaders.py) -- no other interval query param to honor.
    rows = (
        await db.scalars(
            select(PriceHistoryMinute)
            .where(PriceHistoryMinute.ticker == ticker.strip().upper())
            .order_by(PriceHistoryMinute.timestamp.asc())
        )
    ).all()
    return [
        {
            "timestamp": r.timestamp, "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume,
        }
        for r in rows
    ]
