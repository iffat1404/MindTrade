from fastapi import APIRouter

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Endpoints (KYC review, accounts, audit/trade/matching logs, behavioral
# analytics, feed control) land across Sprints 2, 4, 5, and 8.
