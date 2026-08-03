import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account, Fill, Order
from app.services.portfolio_engine import get_portfolio

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/portfolio")
async def portfolio_report(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """P&L statement: realized vs unrealized, per position (FRONTEND_DESIGN_GUIDE's Reports page)."""
    summary = await get_portfolio(db, current_user)
    return {
        "cash_balance": summary.cash_balance,
        "realized_pnl_total": summary.realized_pnl_total,
        "unrealized_pnl_total": summary.unrealized_pnl_total,
        "net_worth": summary.net_worth,
        "positions": [
            {
                "ticker": p.ticker,
                "product_type": p.product_type,
                "signed_qty": p.signed_qty,
                "avg_cost": p.avg_cost,
                "current_price": p.current_price,
                "realized_pnl": p.realized_pnl,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in summary.positions
        ],
    }


@router.get("/portfolio/export")
async def export_portfolio_csv(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    """Trade journal export (CSV): every real (non-backtest) fill for this
    account, one row per fill.
    """
    rows = (
        await db.execute(
            select(Fill, Order)
            .join(Order, Fill.order_id == Order.id)
            .where(Order.account_id == current_user.id, Fill.is_backtest.is_(False))
            .order_by(Fill.timestamp.asc())
        )
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "ticker", "side", "qty", "price", "fees", "realized_pnl"])
    for fill, order in rows:
        writer.writerow(
            [
                fill.timestamp.isoformat(), order.ticker, order.side, fill.fill_qty, fill.fill_price,
                fill.fees, fill.realized_pnl if fill.realized_pnl is not None else "",
            ]
        )
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_journal.csv"},
    )
