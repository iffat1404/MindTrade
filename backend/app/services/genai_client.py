import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Optional

from anthropic import AsyncAnthropic

from app.core.config import settings

if TYPE_CHECKING:
    from app.models.schemas import OrderCreateRequest
    from app.services.behavioral_guard import BiasSignal

logger = logging.getLogger("mindtrade.genai")

_EXTRACTION_PROMPT = """You are extracting identity fields from a KYC (Know Your Customer) document for a trading platform.

Look at the attached identity document (passport, driver's license, or national ID) and extract:
- full_name: the document holder's full legal name, exactly as printed
- dob: date of birth, ISO 8601 (YYYY-MM-DD)
- id_number: the document/ID number
- expiry_date: expiry date, ISO 8601 (YYYY-MM-DD)
- issuing_country: ISO 3166-1 alpha-3 country code (e.g. USA, GBR, IND)
- confidence: your confidence in this extraction as exactly one of "HIGH", "MEDIUM", "LOW"

If a field genuinely cannot be read, use null for that field (but always return a confidence).

Return ONLY a JSON object with exactly these six keys, no other text:
{"full_name": ..., "dob": ..., "id_number": ..., "expiry_date": ..., "issuing_country": ..., "confidence": ...}
"""

_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@dataclass
class KYCExtractionResult:
    full_name: Optional[str] = None
    dob: Optional[date] = None
    id_number: Optional[str] = None
    expiry_date: Optional[date] = None
    issuing_country: Optional[str] = None
    confidence: Optional[str] = None
    extraction_succeeded: bool = False


def _build_content_block(document_bytes: bytes, mime_type: str) -> dict:
    b64 = base64.standard_b64encode(document_bytes).decode("ascii")
    if mime_type in _IMAGE_MIME_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}}
    # PDF: anthropic==0.37.0 (pinned in requirements.txt) predates typed
    # "document" content-block support, but the Messages API accepts a
    # plain dict here. If the server rejects it, extract_kyc_fields's broad
    # except below falls back gracefully rather than ever blocking the
    # submission on this -- not verified against a real API call (no
    # Anthropic API key available in this environment).
    return {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": b64}}


def _parse_date(value: object) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def extract_kyc_fields(document_bytes: bytes, mime_type: str) -> KYCExtractionResult:
    """Extracts KYC fields from an ID document image/PDF via Claude vision.

    Never raises: any failure (missing API key, timeout, malformed
    response, API error) returns a result with extraction_succeeded=False,
    so /api/kyc/submit always falls back to manual admin review rather
    than blocking on GenAI availability.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not configured -- skipping GenAI KYC extraction")
        return KYCExtractionResult()

    try:
        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await asyncio.wait_for(
            client.messages.create(
                model=settings.GENAI_MODEL,
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            _build_content_block(document_bytes, mime_type),
                            {"type": "text", "text": _EXTRACTION_PROMPT},
                        ],
                    }
                ],
            ),
            timeout=settings.GENAI_TIMEOUT_SECONDS,
        )
        payload = json.loads(response.content[0].text)
        return KYCExtractionResult(
            full_name=payload.get("full_name"),
            dob=_parse_date(payload.get("dob")),
            id_number=payload.get("id_number"),
            expiry_date=_parse_date(payload.get("expiry_date")),
            issuing_country=payload.get("issuing_country"),
            confidence=payload.get("confidence"),
            extraction_succeeded=True,
        )
    except Exception:
        logger.exception("GenAI KYC extraction failed; falling back to manual review")
        return KYCExtractionResult()


# ---------------------------------------------------------------------------
# Behavioral Guardrail intervention (Sprint 5, MASTER_BUILD_PLAN Part 2.4)
# ---------------------------------------------------------------------------
_BEHAVIORAL_PROMPT_TEMPLATE = """You are a behavioral finance coach for a trading platform.
A trader is about to place this order:
- Ticker: {ticker}
- Side: {side}
- Quantity: {qty}
- Type: {order_type}
- Product: {product_type}

Their recent trading context:
- Recent trades: {recent_trades}
- Today's P&L: {today_pnl}
- Minutes since last trade: {minutes_since_last_trade}

Detected behavioral signals: {signals}
Behavioral Risk Score: {brs}/100

STRICT RULES:
1. Never say "do not trade" or "this is a bad trade"
2. Never make a price prediction or recommendation
3. Never lecture -- be concise (max 3 sentences)
4. Be specific to THIS trade and THIS pattern, not generic
5. Use first-person language ("You appear to be..." not "Traders often...")
6. Always end with a reflective question for the trader
7. The trader has the right to proceed -- acknowledge that explicitly

Return ONLY a JSON object with exactly these five keys, no other text:
{{"bias_detected": ..., "headline": ..., "explanation": ..., "reflection_question": ..., "educational_fact": ...}}
"""


@dataclass
class BehavioralIntervention:
    bias_detected: Optional[str] = None
    headline: Optional[str] = None
    explanation: Optional[str] = None
    reflection_question: Optional[str] = None
    educational_fact: Optional[str] = None
    trader_can_proceed: bool = True
    acknowledgment_required: bool = False
    # True whenever there's no intervention to show the trader (no API key,
    # API failure, malformed response) -- the guardrail never blocks the
    # trade over a GenAI hiccup (US-5.4), it just quietly skips the modal.
    skip_intervention: bool = False


async def generate_behavioral_intervention(
    *,
    order: "OrderCreateRequest",
    context: dict,
    signals: list["BiasSignal"],
    brs: int,
    acknowledgment_required: bool,
) -> BehavioralIntervention:
    """Generates a personalized, non-judgmental intervention for a
    trader about to place a risky-looking order. BRS and bias detection
    are pure deterministic Python (behavioral_guard.py); Claude only
    writes the explanation text. acknowledgment_required is computed by
    the caller from the BRS band (brs > BRS_WARNING_MAX) rather than
    trusted from Claude's echoed JSON, so it's always correct even if the
    model gets the arithmetic in the prompt wrong.

    Never raises: any failure returns skip_intervention=True so the order
    flow proceeds without an intervention rather than blocking on GenAI
    availability.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not configured -- skipping behavioral intervention")
        return BehavioralIntervention(skip_intervention=True)

    prompt = _BEHAVIORAL_PROMPT_TEMPLATE.format(
        ticker=order.ticker,
        side=order.side,
        qty=order.qty,
        order_type=order.order_type,
        product_type=order.product_type,
        recent_trades=context.get("recent_trades"),
        today_pnl=context.get("today_pnl"),
        minutes_since_last_trade=context.get("minutes_since_last_trade"),
        signals=[{"type": s.type, "score": s.score, "detail": s.detail} for s in signals],
        brs=brs,
    )

    try:
        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await asyncio.wait_for(
            client.messages.create(
                model=settings.GENAI_MODEL,
                max_tokens=settings.GENAI_MAX_TOKENS_BEHAVIORAL,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=settings.GENAI_TIMEOUT_SECONDS,
        )
        payload = json.loads(response.content[0].text)
        return BehavioralIntervention(
            bias_detected=payload.get("bias_detected"),
            headline=payload.get("headline"),
            explanation=payload.get("explanation"),
            reflection_question=payload.get("reflection_question"),
            educational_fact=payload.get("educational_fact"),
            trader_can_proceed=True,
            acknowledgment_required=acknowledgment_required,
        )
    except Exception:
        logger.exception("GenAI behavioral intervention failed; proceeding without intervention")
        return BehavioralIntervention(skip_intervention=True)
