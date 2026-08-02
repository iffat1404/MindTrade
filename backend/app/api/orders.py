from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import get_current_user
from app.core.db import get_db
from app.models.orm import Account, BehavioralScore, Order, OrderEvent
from app.models.schemas import OrderCreateRequest, OrderDetailResponse, OrderResponse
from app.services import behavioral_guard
from app.services.order_engine import get_latest_market_time, validate_order

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _log_event(db: AsyncSession, order: Order, from_state: Optional[str], to_state: str, reason: Optional[str] = None) -> None:
    db.add(OrderEvent(order_id=order.id, from_state=from_state, to_state=to_state, reason=reason))


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreateRequest,
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    reference_time = await get_latest_market_time(db)
    outcome = await validate_order(payload, current_user, db, reference_time=reference_time)

    order = Order(
        account_id=current_user.id,
        ticker=payload.ticker,
        side=payload.side,
        order_type=payload.order_type,
        product_type=payload.product_type,
        qty=payload.qty,
        remaining_qty=payload.qty,
        limit_price=payload.limit_price,
        stop_trigger_price=payload.stop_trigger_price,
        stop_limit_price=payload.stop_limit_price,
        status="NEW",
    )
    db.add(order)
    await db.flush()  # populate order.id for the OrderEvent FK below
    _log_event(db, order, None, "NEW")

    # Sprint 5: link this order back to the behavioral-check score that
    # preceded it (if the trader went through POST /api/genai/behavioral-check
    # first), so the behavioral analytics can see it actually proceeded --
    # regardless of whether validation below ends up rejecting it, since
    # submitting to the pipeline is itself the "proceed" action.
    if payload.behavioral_score_id is not None:
        score = await db.get(BehavioralScore, payload.behavioral_score_id)
        if score is not None and score.account_id == current_user.id:
            score.order_id = order.id
            score.trader_proceeded = True

    if not outcome.passed:
        order.status = "REJECTED"
        _log_event(db, order, "NEW", "REJECTED", reason=f"{outcome.reason_code}: {outcome.message}")
        await db.commit()
        await db.refresh(order)
        response = OrderResponse.model_validate(order)
        response.reason_code = outcome.reason_code
        response.message = outcome.message
        return response

    validated_reason = "Passed all validation checks"
    if outcome.wash_trade_flagged:
        validated_reason += f" (WASH_TRADE_FLAG: {outcome.wash_trade_message})"
    order.status = "VALIDATED"
    _log_event(db, order, "NEW", "VALIDATED", reason=validated_reason)

    # NSE FIFO matching (Sprint 4) picks up ROUTED orders from here.
    order.status = "ROUTED"
    _log_event(db, order, "VALIDATED", "ROUTED", reason="Awaiting NSE matching (Sprint 4)")

    # Behavioral history's overconfidence/overtrading checks count real
    # trading activity, not rejected attempts -- only bump this once an
    # order actually reaches ROUTED.
    await behavioral_guard.record_order_placed(
        db, account_id=current_user.id, market_time=reference_time or datetime.now(timezone.utc)
    )

    await db.commit()
    await db.refresh(order)
    return OrderResponse.model_validate(order)


@router.get("", response_model=list[OrderResponse])
async def list_orders(
    current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[Order]:
    result = await db.scalars(
        select(Order).where(Order.account_id == current_user.id).order_by(Order.created_at.desc())
    )
    return list(result.all())


@router.get("/{order_id}", response_model=OrderDetailResponse)
async def get_order(
    order_id: UUID, current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Order:
    order = await db.scalar(
        select(Order).where(Order.id == order_id).options(selectinload(Order.events))
    )
    if order is None or order.account_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: UUID, current_user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Order:
    order = await db.get(Order, order_id)
    if order is None or order.account_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.status not in ("VALIDATED", "ROUTED"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot cancel an order in status {order.status}"
        )

    from_state = order.status
    order.status = "CANCELLED"
    _log_event(db, order, from_state, "CANCELLED", reason="Cancelled by trader")

    await db.commit()
    await db.refresh(order)
    return order
