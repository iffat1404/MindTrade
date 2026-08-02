from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import create_access_token, get_password_hash, verify_password, verify_token
from app.models.orm import Account
from app.models.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Points Swagger's "Authorize" button at /login; the actual login endpoint
# takes a JSON body (not OAuth2 form-encoded), so this is only used here to
# extract the Bearer token from the Authorization header for protected routes.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> Account:
    existing = await db.scalar(select(Account).where(Account.username == payload.username))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")

    account = Account(
        username=payload.username,
        password_hash=get_password_hash(payload.password),
        role="trader",
        kyc_status="NOT_STARTED",
    )
    if payload.starting_capital is not None:
        account.starting_capital = payload.starting_capital
        account.cash_balance = payload.starting_capital

    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    account = await db.scalar(select(Account).where(Account.username == payload.username))
    if account is None or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token({"sub": str(account.id), "role": account.role})
    return TokenResponse(access_token=token, role=account.role)


# NOTE: HOW_TO_USE_MASTER_BUILD_PLAN.md's Task 1.3/1.4 prompt templates put
# get_current_user() and require_role() in core/security.py. They live here
# instead: get_current_user needs the DB session + Account model, and
# require_role needs get_current_user -- putting either in core/security.py
# (which api/auth.py already imports from) would create a circular import
# between core/security.py and api/auth.py. Later routers should import
# both from here: `from app.api.auth import get_current_user, require_role`.
async def get_current_user(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> Account:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = verify_token(token)
        account_id = UUID(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        raise credentials_error

    account = await db.get(Account, account_id)
    if account is None:
        raise credentials_error
    return account


@router.get("/me", response_model=UserResponse)
async def me(current_user: Account = Depends(get_current_user)) -> Account:
    return current_user
