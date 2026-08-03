from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import require_role
from app.api.websockets import notify_kyc_status_update
from app.core.config import settings
from app.core.db import get_db
from app.models.orm import Account, Fill, KYCSubmission, Order, OrderEvent, OrderMatch
from app.services import behavioral_guard
from app.services.feed_simulator import get_feed_state, pause_feed, reset_feed, start_feed
from app.services.portfolio_engine import get_portfolio

router = APIRouter(prefix="/api/admin", tags=["admin"])


class RejectRequest(BaseModel):
    reason: str


async def _get_submission_or_404(db: AsyncSession, submission_id: UUID) -> KYCSubmission:
    submission = await db.get(KYCSubmission, submission_id)
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KYC submission not found")
    return submission


@router.get("/kyc", dependencies=[Depends(require_role("admin"))])
async def list_kyc_queue(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.scalars(
        select(KYCSubmission)
        .where(KYCSubmission.status == "PENDING_REVIEW")
        .order_by(KYCSubmission.submitted_at)
    )
    return [
        {
            "id": s.id,
            "account_id": s.account_id,
            "id_type": s.id_type,
            "declared_full_name": s.declared_full_name,
            "extracted_full_name": s.extracted_full_name,
            "extracted_dob": s.extracted_dob,
            "extracted_id_number": s.extracted_id_number,
            "extracted_expiry_date": s.extracted_expiry_date,
            "extracted_issuing_country": s.extracted_issuing_country,
            "extraction_confidence": s.extraction_confidence,
            "auto_check_passed": s.auto_check_passed,
            "auto_check_notes": s.auto_check_notes,
            "document_path": s.document_path,
            "submitted_at": s.submitted_at,
        }
        for s in result.all()
    ]


@router.post("/kyc/{submission_id}/approve")
async def approve_kyc(
    submission_id: UUID,
    current_user: Account = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    submission = await _get_submission_or_404(db, submission_id)
    if submission.status != "PENDING_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Submission is already {submission.status}"
        )

    account = await db.get(Account, submission.account_id)

    submission.status = "APPROVED"
    submission.reviewed_by_admin_id = current_user.id
    submission.reviewed_at = datetime.now(timezone.utc)
    account.kyc_status = "APPROVED"

    await db.commit()
    await notify_kyc_status_update(account.id, "APPROVED")

    return {"submission_id": submission.id, "status": submission.status}


@router.post("/kyc/{submission_id}/reject")
async def reject_kyc(
    submission_id: UUID,
    payload: RejectRequest,
    current_user: Account = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    submission = await _get_submission_or_404(db, submission_id)
    if submission.status != "PENDING_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Submission is already {submission.status}"
        )

    account = await db.get(Account, submission.account_id)

    submission.status = "REJECTED"
    submission.review_notes = payload.reason
    submission.reviewed_by_admin_id = current_user.id
    submission.reviewed_at = datetime.now(timezone.utc)
    account.kyc_status = "REJECTED"

    await db.commit()
    await notify_kyc_status_update(account.id, "REJECTED", payload.reason)

    return {"submission_id": submission.id, "status": submission.status}


# ---------------------------------------------------------------------------
# Order matching audit log (Sprint 4 -- "show off NSE FIFO matching" per
# FRONTEND_DESIGN_GUIDE's Order Matching Audit Log page)
# ---------------------------------------------------------------------------
@router.get("/order-matching-logs", dependencies=[Depends(require_role("admin"))])
async def list_order_matching_logs(
    ticker: Optional[str] = None,
    match_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = (
        select(OrderMatch, Order)
        .join(Order, OrderMatch.order_id == Order.id)
        .order_by(OrderMatch.matched_at.desc())
        .limit(limit)
    )
    if ticker:
        stmt = stmt.where(Order.ticker == ticker.strip().upper())
    if match_type:
        stmt = stmt.where(OrderMatch.match_type == match_type)
    if date_from:
        stmt = stmt.where(OrderMatch.matched_at >= date_from)
    if date_to:
        stmt = stmt.where(OrderMatch.matched_at <= date_to)

    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": match.id,
            "order_id": match.order_id,
            "account_id": order.account_id,
            "ticker": order.ticker,
            "side": order.side,
            "matched_at": match.matched_at,
            "match_type": match.match_type,
            "fill_price": match.fill_price,
            "fill_qty": match.fill_qty,
            "counterparty_account_id": match.counterparty_account_id,
            "nse_match_algorithm": match.nse_match_algorithm,
            "matching_log": match.matching_log,
        }
        for match, order in rows
    ]


# ---------------------------------------------------------------------------
# Feed control (Sprint 4 -- Feed Control admin page)
# ---------------------------------------------------------------------------
@router.get("/feed/status", dependencies=[Depends(require_role("admin"))])
async def feed_status(db: AsyncSession = Depends(get_db)) -> dict:
    state = await get_feed_state(db)
    return {
        "timestamp": state.current_tick_time,
        "is_running": state.is_running,
        "speed": state.speed_multiplier,
        "tickers": sorted(settings.SECTOR_BY_TICKER.keys()),
    }


@router.post("/feed/start", dependencies=[Depends(require_role("admin"))])
async def feed_start(db: AsyncSession = Depends(get_db)) -> dict:
    state = await start_feed(db)
    return {"timestamp": state.current_tick_time, "is_running": state.is_running}


@router.post("/feed/pause", dependencies=[Depends(require_role("admin"))])
async def feed_pause(db: AsyncSession = Depends(get_db)) -> dict:
    state = await pause_feed(db)
    return {"timestamp": state.current_tick_time, "is_running": state.is_running}


@router.post("/feed/reset", dependencies=[Depends(require_role("admin"))])
async def feed_reset(db: AsyncSession = Depends(get_db)) -> dict:
    state = await reset_feed(db)
    return {"timestamp": state.current_tick_time, "is_running": state.is_running}


# ---------------------------------------------------------------------------
# Behavioral analytics (Sprint 5 -- Part 2.7)
# ---------------------------------------------------------------------------
@router.get("/behavioral-analytics", dependencies=[Depends(require_role("admin"))])
async def platform_behavioral_analytics(db: AsyncSession = Depends(get_db)) -> dict:
    return await behavioral_guard.get_platform_analytics(db)


@router.get("/behavioral-analytics/{account_id}", dependencies=[Depends(require_role("admin"))])
async def account_behavioral_analytics(account_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return await behavioral_guard.get_account_analytics(db, account)


# ---------------------------------------------------------------------------
# Accounts overview (Sprint 8, Task 8.1 / US-8.1)
# ---------------------------------------------------------------------------
@router.get("/accounts", dependencies=[Depends(require_role("admin"))])
async def list_accounts(
    kyc_status: Optional[str] = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(Account).where(Account.role == "trader")
    if kyc_status and kyc_status.lower() != "all":
        stmt = stmt.where(Account.kyc_status == kyc_status.upper())
    accounts = (await db.scalars(stmt)).all()

    results = []
    for account in accounts:
        portfolio = await get_portfolio(db, account)
        order_count = await db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.account_id == account.id, Order.is_backtest.is_(False))
        )
        results.append(
            {
                "id": account.id,
                "username": account.username,
                "role": account.role,
                "kyc_status": account.kyc_status,
                "cash_balance": account.cash_balance,
                "net_worth": portfolio.net_worth,
                "order_count": order_count,
            }
        )
    results.sort(key=lambda r: r["net_worth"], reverse=True)
    return results


@router.get("/accounts/{account_id}", dependencies=[Depends(require_role("admin"))])
async def account_detail(account_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Not in the doc's Task 8.1 (only the list endpoint is spelled out),
    but FRONTEND_DESIGN_GUIDE's Accounts Overview page explicitly wants a
    "click row -> account detail view" with holdings + recent orders, and
    there's no other way for an admin to see another trader's
    holdings/orders (the trader-scoped /api/portfolio and /api/orders only
    return the current user's own data) -- same pattern as prior sprints
    adding an endpoint the UI clearly needs beyond the doc's minimum.
    """
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    portfolio = await get_portfolio(db, account)
    recent_orders = (
        await db.scalars(
            select(Order)
            .where(Order.account_id == account_id, Order.is_backtest.is_(False))
            .order_by(Order.created_at.desc())
            .limit(20)
        )
    ).all()

    return {
        "id": account.id,
        "username": account.username,
        "role": account.role,
        "kyc_status": account.kyc_status,
        "cash_balance": account.cash_balance,
        "net_worth": portfolio.net_worth,
        "positions": [
            {
                "ticker": p.ticker, "product_type": p.product_type, "signed_qty": p.signed_qty,
                "avg_cost": p.avg_cost, "unrealized_pnl": p.unrealized_pnl, "realized_pnl": p.realized_pnl,
            }
            for p in portfolio.positions
        ],
        "recent_orders": [
            {
                "id": o.id, "ticker": o.ticker, "side": o.side, "order_type": o.order_type,
                "status": o.status, "created_at": o.created_at,
            }
            for o in recent_orders
        ],
    }


# ---------------------------------------------------------------------------
# Audit log inspector (Sprint 8, Task 8.1 / US-8.2)
# ---------------------------------------------------------------------------
@router.get("/audit-logs", dependencies=[Depends(require_role("admin"))])
async def list_audit_logs(
    account_id: Optional[UUID] = None,
    ticker: Optional[str] = None,
    order_status: Optional[str] = Query(default=None, alias="status"),
    reason_code: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    is_backtest: Optional[bool] = None,
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = (
        select(Order)
        .options(selectinload(Order.events))
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if account_id:
        stmt = stmt.where(Order.account_id == account_id)
    if ticker:
        stmt = stmt.where(Order.ticker == ticker.strip().upper())
    if order_status:
        stmt = stmt.where(Order.status == order_status.upper())
    if date_from:
        stmt = stmt.where(Order.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Order.created_at <= date_to)
    if is_backtest is not None:
        stmt = stmt.where(Order.is_backtest == is_backtest)

    orders = (await db.scalars(stmt)).unique().all()

    results = []
    for order in orders:
        # reason_code is embedded as a "CODE: message" prefix in whichever
        # event carried it (REJECTED, or VALIDATED when wash-trade-flagged)
        # -- there's no dedicated reason_code column on Order or OrderEvent.
        events = sorted(order.events, key=lambda e: e.timestamp)
        order_reason_code = None
        for event in events:
            if event.reason and ":" in event.reason:
                order_reason_code = event.reason.split(":", 1)[0].strip()
        if reason_code and order_reason_code != reason_code:
            continue

        results.append(
            {
                "order_id": order.id,
                "account_id": order.account_id,
                "ticker": order.ticker,
                "status": order.status,
                "created_at": order.created_at,
                "reason_code": order_reason_code,
                "is_backtest": order.is_backtest,
                "events": [
                    {
                        "from_state": e.from_state, "to_state": e.to_state, "reason": e.reason,
                        "timestamp": e.timestamp,
                    }
                    for e in events
                ],
            }
        )
    return results


# ---------------------------------------------------------------------------
# Trade log inspector (Sprint 8, Task 8.1)
# ---------------------------------------------------------------------------
@router.get("/trade-logs", dependencies=[Depends(require_role("admin"))])
async def list_trade_logs(
    account_id: Optional[UUID] = None,
    ticker: Optional[str] = None,
    product_type: Optional[str] = None,
    pnl: Optional[str] = Query(default=None, description="all | profit | loss"),
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    is_backtest: Optional[bool] = None,
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = (
        select(Fill, Order)
        .join(Order, Fill.order_id == Order.id)
        .order_by(Fill.timestamp.desc())
        .limit(limit)
    )
    if account_id:
        stmt = stmt.where(Order.account_id == account_id)
    if ticker:
        stmt = stmt.where(Order.ticker == ticker.strip().upper())
    if product_type:
        stmt = stmt.where(Order.product_type == product_type.upper())
    if date_from:
        stmt = stmt.where(Fill.timestamp >= date_from)
    if date_to:
        stmt = stmt.where(Fill.timestamp <= date_to)
    if is_backtest is not None:
        stmt = stmt.where(Fill.is_backtest == is_backtest)
    if pnl == "profit":
        stmt = stmt.where(Fill.realized_pnl > 0)
    elif pnl == "loss":
        stmt = stmt.where(Fill.realized_pnl < 0)

    rows = (await db.execute(stmt)).all()

    results = []
    for fill, order in rows:
        # Most recent OrderMatch for this order -- there's no direct FK
        # between a specific Fill and its OrderMatch row (both are created
        # together in _settle_fill but share only order_id), so for orders
        # with multiple partial fills this is an approximation, not a
        # guaranteed exact match.
        match = await db.scalar(
            select(OrderMatch).where(OrderMatch.order_id == order.id).order_by(OrderMatch.matched_at.desc())
        )
        results.append(
            {
                "fill_id": fill.id,
                "timestamp": fill.timestamp,
                "account_id": order.account_id,
                "ticker": order.ticker,
                "side": order.side,
                "qty": fill.fill_qty,
                "fill_price": fill.fill_price,
                "realized_pnl": fill.realized_pnl,
                "product_type": order.product_type,
                "is_backtest": fill.is_backtest,
                "matching_log": match.matching_log if match else None,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Compliance flags (Sprint 8, Task 8's "wash-trade + KYC auto-check failures")
# ---------------------------------------------------------------------------
@router.get("/flags", dependencies=[Depends(require_role("admin"))])
async def list_compliance_flags(
    limit: int = Query(default=100, le=500), db: AsyncSession = Depends(get_db)
) -> dict:
    """Wash-trade flags have no dedicated table -- check_wash_trade (Sprint
    3) never blocks an order, it only annotates the VALIDATED event's
    reason with a "WASH_TRADE_FLAG: ..." marker (see orders.py), so this
    reconstructs the flag list by searching OrderEvent.reason rather than
    querying a real flags table.
    """
    wash_trade_rows = (
        await db.execute(
            select(OrderEvent, Order)
            .join(Order, OrderEvent.order_id == Order.id)
            .where(OrderEvent.reason.ilike("%WASH_TRADE_FLAG%"))
            .order_by(OrderEvent.timestamp.desc())
            .limit(limit)
        )
    ).all()

    kyc_failures = (
        await db.scalars(
            select(KYCSubmission)
            .where(KYCSubmission.auto_check_passed.is_(False))
            .order_by(KYCSubmission.submitted_at.desc())
            .limit(limit)
        )
    ).all()

    return {
        "wash_trade_flags": [
            {
                "order_id": order.id, "account_id": order.account_id, "ticker": order.ticker,
                "timestamp": event.timestamp, "reason": event.reason,
            }
            for event, order in wash_trade_rows
        ],
        "kyc_auto_check_failures": [
            {
                "submission_id": s.id, "account_id": s.account_id, "status": s.status,
                "auto_check_notes": s.auto_check_notes, "submitted_at": s.submitted_at,
            }
            for s in kyc_failures
        ],
    }
