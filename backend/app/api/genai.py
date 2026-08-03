from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models.orm import Account, BehavioralScore
from app.models.schemas import (
    BehavioralCheckResponse,
    BehavioralInterventionResponse,
    BehavioralOutcomeRequest,
    BiasSignalResponse,
    OrderCreateRequest,
    OrderParseRequest,
    OrderParseResponse,
)
from app.services import behavioral_guard, genai_client
from app.services.order_engine import get_latest_market_time

router = APIRouter(prefix="/api/genai", tags=["genai"])


@router.post("/behavioral-check", response_model=BehavioralCheckResponse)
async def behavioral_check(
    payload: OrderCreateRequest,
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BehavioralCheckResponse:
    """Called before order submission (US-5.1). Scores the draft order
    against the trader's rolling behavioral history (deterministic, pure
    Python), and -- if the score is above CLEAR -- asks Claude for a
    personalized intervention. Never blocks the trade: proceed is always
    True, the trader decides.
    """
    market_time = await get_latest_market_time(db) or datetime.now(timezone.utc)
    history = await behavioral_guard.get_or_create_history(db, current_user.id)
    bias_report = await behavioral_guard.calculate_brs(
        db, account_id=current_user.id, order=payload, history=history, market_time=market_time
    )
    level = behavioral_guard.risk_level(bias_report.brs)

    intervention_response = None
    intervention_shown = False
    intervention_text = None

    if bias_report.brs > settings.BRS_CLEAR_MAX:
        context = behavioral_guard.build_intervention_context(history, market_time)
        intervention = await genai_client.generate_behavioral_intervention(
            order=payload,
            context=context,
            signals=bias_report.signals,
            brs=bias_report.brs,
            acknowledgment_required=(level == "HIGH_RISK"),
        )
        if not intervention.skip_intervention:
            intervention_shown = True
            intervention_text = intervention.explanation
            intervention_response = BehavioralInterventionResponse(
                bias_detected=intervention.bias_detected,
                headline=intervention.headline,
                explanation=intervention.explanation,
                reflection_question=intervention.reflection_question,
                educational_fact=intervention.educational_fact,
                trader_can_proceed=intervention.trader_can_proceed,
                acknowledgment_required=intervention.acknowledgment_required,
            )

    score = BehavioralScore(
        account_id=current_user.id,
        brs=bias_report.brs,
        biases_detected=[{"type": s.type, "score": s.score, "detail": s.detail} for s in bias_report.signals],
        intervention_shown=intervention_shown,
        intervention_text=intervention_text,
        calculated_at=market_time,
    )
    db.add(score)
    await db.commit()
    await db.refresh(score)

    return BehavioralCheckResponse(
        id=score.id,
        brs=bias_report.brs,
        risk_level=level,
        signals=[BiasSignalResponse.model_validate(s) for s in bias_report.signals],
        intervention=intervention_response,
        proceed=True,
    )


@router.post("/behavioral-check/{score_id}/outcome")
async def behavioral_check_outcome(
    score_id: UUID,
    payload: BehavioralOutcomeRequest,
    current_user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Records what the trader actually did with an intervention (US-5.1's
    modal Cancel/Proceed buttons). Not in MASTER_BUILD_PLAN's endpoint list
    -- added so the "8 times you proceeded / 4 times you cancelled" stats
    on the behavioral profile (Part 2.6) have somewhere to come from,
    matching prior sprints' pattern of adding endpoints the UI clearly
    needs beyond the doc's minimum (e.g. Sprint 3/4's extra GET/cancel and
    start/pause endpoints). A "proceeded" outcome is also recorded
    automatically by POST /api/orders when the order carries this score's
    id (see behavioral_score_id on OrderCreateRequest) -- this endpoint is
    what a "Cancel" click, which never reaches order creation, uses.
    """
    score = await db.get(BehavioralScore, score_id)
    if score is None or score.account_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Behavioral score not found")

    score.trader_proceeded = payload.proceeded
    if payload.acknowledged is not None:
        score.trader_acknowledged = payload.acknowledged

    await db.commit()
    return {
        "id": score.id,
        "trader_proceeded": score.trader_proceeded,
        "trader_acknowledged": score.trader_acknowledged,
    }


@router.post("/parse-order", response_model=OrderParseResponse)
async def parse_order(
    payload: OrderParseRequest, current_user: Account = Depends(get_current_user)
) -> OrderParseResponse:
    """US-6.4: turns free text like "buy 100 apple at market" into a draft
    order. Always a draft -- the trader must review and explicitly submit
    via POST /api/orders themselves; this endpoint never places a trade.
    """
    result = await genai_client.parse_order_text(payload.text)
    return OrderParseResponse.model_validate(result)
