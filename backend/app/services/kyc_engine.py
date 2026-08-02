from dataclasses import dataclass
from datetime import date
from typing import Optional

from fuzzywuzzy import fuzz

from app.core.config import settings


@dataclass
class CheckResult:
    passed: bool
    detail: str


def check_expiry(extracted_expiry_date: Optional[date], *, today: Optional[date] = None) -> CheckResult:
    today = today or date.today()
    if extracted_expiry_date is None:
        return CheckResult(False, "No expiry date could be extracted from the document")
    if extracted_expiry_date <= today:
        return CheckResult(False, f"Document expired on {extracted_expiry_date.isoformat()}")
    return CheckResult(True, f"Document valid until {extracted_expiry_date.isoformat()}")


def check_age(extracted_dob: Optional[date], *, today: Optional[date] = None) -> CheckResult:
    today = today or date.today()
    if extracted_dob is None:
        return CheckResult(False, "No date of birth could be extracted from the document")
    age = today.year - extracted_dob.year - ((today.month, today.day) < (extracted_dob.month, extracted_dob.day))
    if age < settings.KYC_MIN_AGE_YEARS:
        return CheckResult(False, f"Trader is {age} years old, below the minimum of {settings.KYC_MIN_AGE_YEARS}")
    return CheckResult(True, f"Trader is {age} years old")


def check_name_match(declared_full_name: str, extracted_full_name: Optional[str]) -> CheckResult:
    if not extracted_full_name:
        return CheckResult(False, "No name could be extracted from the document")
    score = fuzz.ratio(declared_full_name.strip().lower(), extracted_full_name.strip().lower())
    if score < settings.KYC_NAME_MATCH_MIN_SCORE:
        return CheckResult(
            False,
            f"Declared name and extracted name are only {score}% similar "
            f"(need >= {settings.KYC_NAME_MATCH_MIN_SCORE}%)",
        )
    return CheckResult(True, f"Declared name and extracted name are {score}% similar")


def check_extraction_confidence(extraction_confidence: Optional[str]) -> CheckResult:
    if extraction_confidence not in ("HIGH", "MEDIUM"):
        return CheckResult(False, f"Extraction confidence too low ({extraction_confidence or 'unknown'})")
    return CheckResult(True, f"Extraction confidence: {extraction_confidence}")


def run_auto_checks(
    *,
    declared_full_name: str,
    extracted_full_name: Optional[str],
    extracted_dob: Optional[date],
    extracted_expiry_date: Optional[date],
    extraction_confidence: Optional[str],
    today: Optional[date] = None,
) -> tuple[bool, dict]:
    """Runs all 4 deterministic KYC auto-checks (expiry, age, name_match,
    extraction_confidence -- pure Python, no AI). Returns (all_passed, notes)
    where notes is a JSON-serializable dict for KYCSubmission.auto_check_notes.

    This is informational for the admin reviewer, not authoritative: a
    submission always lands in PENDING_REVIEW regardless of outcome here --
    only an admin's approve/reject decision changes its status.
    """
    checks = {
        "expiry": check_expiry(extracted_expiry_date, today=today),
        "age": check_age(extracted_dob, today=today),
        "name_match": check_name_match(declared_full_name, extracted_full_name),
        "extraction_confidence": check_extraction_confidence(extraction_confidence),
    }
    all_passed = all(c.passed for c in checks.values())
    notes = {name: {"passed": c.passed, "detail": c.detail} for name, c in checks.items()}
    return all_passed, notes
