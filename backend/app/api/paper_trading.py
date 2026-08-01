from fastapi import APIRouter

router = APIRouter(prefix="/api/backtest", tags=["paper-trading"])

# Endpoints (run backtest, results, trade log) land in Sprint 7.
