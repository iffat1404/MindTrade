import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin, analytics, auth, genai, kyc, orders, paper_trading, portfolio, reports
from app.core.config import settings
from app.core.db import close_db

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("mindtrade")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic (see backend/start.sh), not created here.
    # TODO(Sprint 1): call the data loaders (daily/minute/sentiment CSV+JSON
    # ingestion) and seed_demo.seed_database() here once those modules exist.
    logger.info("MindTrade Platform starting up (environment=%s)", settings.ENVIRONMENT)
    yield
    await close_db()
    logger.info("MindTrade Platform shut down")


app = FastAPI(title="MindTrade Platform", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(status.HTTP_404_NOT_FOUND)
async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Resource not found"})


@app.exception_handler(status.HTTP_422_UNPROCESSABLE_ENTITY)
async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


@app.exception_handler(status.HTTP_500_INTERNAL_SERVER_ERROR)
async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Something went wrong, try again"},
    )


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok"}


# Routers are empty scaffolding for now (see app/api/*.py) -- each sprint
# fills in its own endpoints per MASTER_BUILD_PLAN Part 4.
app.include_router(auth.router)
app.include_router(kyc.router)
app.include_router(admin.router)
app.include_router(orders.router)
app.include_router(portfolio.router)
app.include_router(reports.router)
app.include_router(analytics.router)
app.include_router(paper_trading.router)
app.include_router(genai.router)
