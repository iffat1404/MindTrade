from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_role
from app.api.websockets import notify_kyc_status_update
from app.core.db import get_db
from app.models.orm import Account, KYCSubmission

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Endpoints beyond KYC review (accounts, audit/trade/matching logs,
# behavioral analytics, feed control) land across Sprints 4, 5, and 8.


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
