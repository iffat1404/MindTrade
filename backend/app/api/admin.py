from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_role
from app.api.websockets import notify_kyc_status_update
from app.core.config import settings
from app.core.db import get_db
from app.models.orm import Account, KYCSubmission, Order, OrderMatch
from app.services.feed_simulator import get_feed_state, pause_feed, reset_feed, start_feed

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Endpoints beyond KYC review, matching logs, and feed control (accounts,
# audit/trade logs, behavioral analytics) land across Sprints 5 and 8.


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
