from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account, BacktestRun, Fill, Order, Strategy
from app.models.schemas import (
    BacktestCreateResponse,
    BacktestRequest,
    BacktestResultsResponse,
    BacktestTradeResponse,
)
from app.services import backtest_engine

router = APIRouter(prefix="/api/backtest", tags=["paper-trading"])


async def _get_owned_run(db: AsyncSession, backtest_id: UUID, account_id: UUID) -> tuple[BacktestRun, Strategy]:
    run = await db.get(BacktestRun, backtest_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest not found")
    strategy = await db.get(Strategy, run.strategy_id)
    if strategy is None or strategy.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest not found")
    return run, strategy


@router.post("", response_model=BacktestCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_backtest(
    payload: BacktestRequest,
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BacktestCreateResponse:
    try:
        result = await backtest_engine.run_backtest(
            db,
            account_id=current_user.id,
            ticker=payload.ticker,
            entry_rule=payload.entry_rule,
            exit_rule=payload.exit_rule,
            position_size=payload.position_size,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await db.commit()
    return BacktestCreateResponse(backtest_id=result.backtest_run_id)


@router.get("/{backtest_id}/results", response_model=BacktestResultsResponse)
async def get_backtest_results(
    backtest_id: UUID, current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BacktestResultsResponse:
    run, strategy = await _get_owned_run(db, backtest_id, current_user.id)
    return BacktestResultsResponse(
        backtest_id=run.id,
        ticker=strategy.ticker,
        entry_rule=strategy.entry_rule,
        exit_rule=strategy.exit_rule,
        total_return=run.total_return,
        max_drawdown=run.max_drawdown,
        win_rate=run.win_rate,
        benchmark_return=run.benchmark_return,
        sharpe_ratio=run.sharpe_ratio,
        equity_curve=run.equity_curve or [],
    )


@router.get("/{backtest_id}/trades", response_model=list[BacktestTradeResponse])
async def get_backtest_trades(
    backtest_id: UUID, current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[BacktestTradeResponse]:
    await _get_owned_run(db, backtest_id, current_user.id)

    orders = (
        await db.scalars(
            select(Order).where(Order.backtest_run_id == backtest_id).order_by(Order.created_at.asc())
        )
    ).all()

    trades: list[BacktestTradeResponse] = []
    for order in orders:
        fill = await db.scalar(select(Fill).where(Fill.order_id == order.id))
        if fill is None:
            continue
        trades.append(
            BacktestTradeResponse(
                side=order.side, date=fill.timestamp.date(), price=fill.fill_price,
                qty=fill.fill_qty, realized_pnl=fill.realized_pnl,
            )
        )
    return trades
