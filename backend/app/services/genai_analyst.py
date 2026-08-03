import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from anthropic import AsyncAnthropic

from app.core.config import settings

if TYPE_CHECKING:
    from app.services.session_analyzer import CriticalMoment

logger = logging.getLogger("mindtrade.genai")

# Task 6.2 (MASTER_BUILD_PLAN Sprint 6): one Claude call per critical
# moment (not one combined call for all 3) -- each moment gets its own
# focused analysis + lesson.
_MOMENT_ANALYSIS_PROMPT_TEMPLATE = """Analyze this trading moment in 3-4 sentences:
- What bias fired
- What it cost them
- ONE specific lesson to fix this pattern

Be direct, not preachy. Focus on patterns, not shame.

Moment details:
- Ticker: {ticker}
- Side: {side}
- Timestamp: {timestamp}
- Bias detected: {bias_name}
- Behavioral Risk Score: {brs}/100
- Guardrail intervention shown: {intervention_shown}
- Trader's response: {trader_response}
- Realized P&L from this trade: {realized_pnl}

Return ONLY a JSON object with exactly these two keys, no other text:
{{"analysis": "...", "lesson": "..."}}
"""


@dataclass
class MomentAnalysis:
    analysis: Optional[str] = None
    lesson: Optional[str] = None
    # True whenever there's no analysis to show (no API key, timeout,
    # malformed response) -- Session Review degrades to showing the raw
    # moment data without Claude's narration rather than failing the
    # whole page.
    skip: bool = False


def _trader_response_text(moment: "CriticalMoment") -> str:
    if not moment.intervention_shown:
        return "no intervention was shown"
    if moment.trader_proceeded is False:
        return "listened and cancelled"
    if moment.trader_proceeded is True:
        return "proceeded anyway"
    return "outcome not recorded"


async def generate_moment_analysis(moment: "CriticalMoment") -> MomentAnalysis:
    """Generates the plain-English analysis + one actionable lesson for a
    single critical moment (Task 6.2). Never raises: any failure returns
    skip=True so Session Review still renders the moment's raw data.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not configured -- skipping moment analysis")
        return MomentAnalysis(skip=True)

    prompt = _MOMENT_ANALYSIS_PROMPT_TEMPLATE.format(
        ticker=moment.ticker,
        side=moment.side,
        timestamp=moment.timestamp.isoformat(),
        bias_name=moment.bias_name or "none detected",
        brs=moment.brs,
        intervention_shown=moment.intervention_shown,
        trader_response=_trader_response_text(moment),
        realized_pnl=moment.realized_pnl if moment.realized_pnl is not None else "unknown",
    )

    try:
        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await asyncio.wait_for(
            client.messages.create(
                model=settings.GENAI_MODEL,
                max_tokens=settings.GENAI_MAX_TOKENS_SESSION_REVIEW,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=settings.GENAI_TIMEOUT_SECONDS,
        )
        payload = json.loads(response.content[0].text)
        return MomentAnalysis(analysis=payload.get("analysis"), lesson=payload.get("lesson"))
    except Exception:
        logger.exception("GenAI moment analysis failed; falling back to raw moment data")
        return MomentAnalysis(skip=True)
