import re
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=8)
    starting_capital: Optional[Decimal] = None

    @field_validator("username")
    @classmethod
    def username_alphanumeric_underscore(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", v):
            raise ValueError("username must contain only letters, numbers, and underscores")
        return v

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        # Per FRONTEND_DESIGN_GUIDE Part 9: min 8 chars, 1 uppercase, 1 number.
        if not re.search(r"[A-Z]", v) or not re.search(r"\d", v):
            raise ValueError("password must contain at least one uppercase letter and one number")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class UserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    kyc_status: str
    cash_balance: Decimal

    model_config = {"from_attributes": True}
