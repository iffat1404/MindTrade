import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models.orm import Account, KYCSubmission
from app.services import kyc_engine
from app.services.genai_client import extract_kyc_fields

logger = logging.getLogger("mindtrade.kyc")

router = APIRouter(prefix="/api/kyc", tags=["kyc"])

_ALLOWED_ID_TYPES = {"PASSPORT", "DRIVERS_LICENSE", "NATIONAL_ID"}
_EXTENSION_BY_MIME = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}


def _upload_dir() -> Path:
    path = Path(settings.KYC_UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


@router.post("/submit", status_code=status.HTTP_201_CREATED)
async def submit_kyc(
    id_type: str = Form(...),
    declared_full_name: str = Form(...),
    file: UploadFile = File(...),
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if id_type not in _ALLOWED_ID_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"id_type must be one of {sorted(_ALLOWED_ID_TYPES)}",
        )

    if file.content_type not in settings.KYC_ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type {file.content_type!r}; allowed: {settings.KYC_ALLOWED_MIME_TYPES}",
        )

    document_bytes = await file.read()
    max_bytes = settings.KYC_MAX_FILE_SIZE_MB * 1024 * 1024
    if len(document_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds the {settings.KYC_MAX_FILE_SIZE_MB}MB limit",
        )
    if len(document_bytes) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    extension = _EXTENSION_BY_MIME[file.content_type]
    filename = f"{current_user.id}_{uuid.uuid4().hex}.{extension}"
    document_path = _upload_dir() / filename
    document_path.write_bytes(document_bytes)

    extraction = await extract_kyc_fields(document_bytes, file.content_type)

    auto_check_passed, auto_check_notes = kyc_engine.run_auto_checks(
        declared_full_name=declared_full_name,
        extracted_full_name=extraction.full_name,
        extracted_dob=extraction.dob,
        extracted_expiry_date=extraction.expiry_date,
        extraction_confidence=extraction.confidence,
    )

    submission = KYCSubmission(
        account_id=current_user.id,
        id_type=id_type,
        document_path=str(document_path),
        declared_full_name=declared_full_name,
        extracted_full_name=extraction.full_name,
        extracted_dob=extraction.dob,
        extracted_id_number=extraction.id_number,
        extracted_expiry_date=extraction.expiry_date,
        extracted_issuing_country=extraction.issuing_country,
        extraction_confidence=extraction.confidence,
        auto_check_passed=auto_check_passed,
        auto_check_notes=auto_check_notes,
        status="PENDING_REVIEW",
    )
    db.add(submission)
    current_user.kyc_status = "PENDING_REVIEW"

    await db.commit()
    await db.refresh(submission)

    return {"submission_id": submission.id, "status": submission.status}


@router.get("/status")
async def kyc_status(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    latest = await db.scalar(
        select(KYCSubmission)
        .where(KYCSubmission.account_id == current_user.id)
        .order_by(KYCSubmission.submitted_at.desc())
    )
    return {
        "kyc_status": current_user.kyc_status,
        "submission_details": (
            {
                "id": latest.id,
                "status": latest.status,
                "submitted_at": latest.submitted_at,
                "auto_check_passed": latest.auto_check_passed,
                "review_notes": latest.review_notes,
            }
            if latest is not None
            else None
        ),
    }
