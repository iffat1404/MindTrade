from fastapi import APIRouter

router = APIRouter(prefix="/api/genai", tags=["genai"])

# Endpoints (behavioral-check, parse-order, explain, portfolio-summary,
# explain-rejection) land across Sprints 5 and 6.
