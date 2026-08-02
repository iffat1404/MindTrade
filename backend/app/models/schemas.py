import re
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


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


# ---------------------------------------------------------------------------
# Orders (Sprint 3)
# ---------------------------------------------------------------------------
class OrderCreateRequest(BaseModel):
    """Shape/structural validation only. Business rules (KYC, buying power,
    price collar, ...) are the order engine's 12-check chain, which returns
    a reason_code rather than a 422 -- these validators only reject orders
    that are malformed regardless of account or market state.
    """

    # JSON field is "type" (per FRONTEND_DESIGN_GUIDE's order form + the
    # behavioral-guard snippets in MASTER_BUILD_PLAN), but `type` shadows a
    # builtin, so it's aliased to order_type internally -- matching the same
    # rename already used on the Order ORM model.
    model_config = {"populate_by_name": True}

    ticker: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = Field(alias="type")
    product_type: Literal["CNC", "MIS"] = "CNC"
    qty: int = Field(gt=0)
    limit_price: Optional[Decimal] = Field(default=None, gt=0)
    stop_trigger_price: Optional[Decimal] = Field(default=None, gt=0)
    stop_limit_price: Optional[Decimal] = Field(default=None, gt=0)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def required_prices_per_order_type(self) -> "OrderCreateRequest":
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type in ("SL", "SL-M") and self.stop_trigger_price is None:
            raise ValueError("stop_trigger_price is required for SL and SL-M orders")
        if self.order_type == "SL" and self.stop_limit_price is None:
            raise ValueError("stop_limit_price is required for SL orders")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("limit_price must not be set for MARKET orders")
        return self


class OrderResponse(BaseModel):
    id: UUID
    ticker: str
    side: str
    order_type: str = Field(serialization_alias="type")
    product_type: str
    qty: int
    remaining_qty: int
    limit_price: Optional[Decimal]
    stop_trigger_price: Optional[Decimal]
    stop_limit_price: Optional[Decimal]
    status: str
    created_at: datetime
    # Populated by POST /api/orders when the 12-check chain rejects the
    # order (per FRONTEND_DESIGN_GUIDE's {id, status, reason_code?}
    # response shape) -- None on a normal GET or a passing order.
    reason_code: Optional[str] = None
    message: Optional[str] = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class OrderEventResponse(BaseModel):
    from_state: Optional[str]
    to_state: str
    reason: Optional[str]
    timestamp: datetime

    model_config = {"from_attributes": True}


class OrderDetailResponse(OrderResponse):
    events: list[OrderEventResponse] = []


# ---------------------------------------------------------------------------
# Portfolio (Sprint 4)
# ---------------------------------------------------------------------------
class PositionLotResponse(BaseModel):
    side: str
    qty_remaining: int
    cost_price: Decimal
    opened_at: datetime

    model_config = {"from_attributes": True}


class PositionSummaryResponse(BaseModel):
    ticker: str
    product_type: str
    signed_qty: int
    avg_cost: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    sector: str
    lots: list[PositionLotResponse] = []

    model_config = {"from_attributes": True}


class PortfolioResponse(BaseModel):
    cash_balance: Decimal
    margin_used: Decimal
    positions: list[PositionSummaryResponse]
    market_value_total: Decimal
    net_worth: Decimal
    unrealized_pnl_total: Decimal
    realized_pnl_total: Decimal
    cagr: Optional[Decimal]

    model_config = {"from_attributes": True}


class SectorExposureItem(BaseModel):
    sector: str
    market_value: Decimal
    pct_of_net_worth: Decimal


class SectorExposureResponse(BaseModel):
    sectors: list[SectorExposureItem]
