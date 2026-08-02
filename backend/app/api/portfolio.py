from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account
from app.models.schemas import PortfolioResponse, PositionSummaryResponse, SectorExposureResponse
from app.services.portfolio_engine import get_portfolio, get_sector_exposure

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("", response_model=PortfolioResponse)
async def portfolio_summary(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PortfolioResponse:
    summary = await get_portfolio(db, current_user)
    return PortfolioResponse.model_validate(summary)


@router.get("/positions", response_model=list[PositionSummaryResponse])
async def portfolio_positions(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[PositionSummaryResponse]:
    # include_lots=True: this endpoint is specifically for the Portfolio
    # page's "FIFO lots detail (expandable per position)" panel, while the
    # summary endpoint above stays lighter for dashboard/trade-page use.
    summary = await get_portfolio(db, current_user, include_lots=True)
    return [PositionSummaryResponse.model_validate(p) for p in summary.positions]


@router.get("/exposure", response_model=SectorExposureResponse)
async def portfolio_exposure(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> SectorExposureResponse:
    sectors = await get_sector_exposure(db, current_user)
    return SectorExposureResponse(sectors=sectors)
