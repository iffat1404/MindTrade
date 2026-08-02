"""Unit tests for genai_client.generate_behavioral_intervention: graceful
degradation when no API key is configured (US-5.4), and the happy path with
the Anthropic client mocked out (no real API key is available in this
environment, same constraint noted in genai_client.py's KYC extraction code).
"""

import json
import types

import pytest

from app.core.config import settings
from app.services import genai_client
from app.services.behavioral_guard import BiasSignal

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_order():
    return types.SimpleNamespace(ticker="AAPL", side="BUY", order_type="MARKET", qty=10, product_type="CNC")


async def test_skips_intervention_when_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    result = await genai_client.generate_behavioral_intervention(
        order=_fake_order(),
        context={"recent_trades": [], "today_pnl": "0", "minutes_since_last_trade": None},
        signals=[BiasSignal("REVENGE_TRADING", 30, "test detail")],
        brs=80,
        acknowledgment_required=True,
    )
    assert result.skip_intervention is True
    assert result.headline is None


async def test_generates_intervention_on_mocked_success(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")

    payload = {
        "bias_detected": "REVENGE_TRADING",
        "headline": "Quick check before you proceed",
        "explanation": "You are trading right after a loss, larger than usual.",
        "reflection_question": "Are you trading your plan or your mood?",
        "educational_fact": "Revenge trading after a loss often compounds losses.",
    }

    class _FakeContentBlock:
        text = json.dumps(payload)

    class _FakeResponse:
        content = [_FakeContentBlock()]

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeAsyncAnthropic:
        def __init__(self, api_key):
            self.messages = _FakeMessages()

    monkeypatch.setattr(genai_client, "AsyncAnthropic", _FakeAsyncAnthropic)

    result = await genai_client.generate_behavioral_intervention(
        order=_fake_order(),
        context={"recent_trades": [], "today_pnl": "-500", "minutes_since_last_trade": 2.0},
        signals=[BiasSignal("REVENGE_TRADING", 30, "test detail")],
        brs=80,
        acknowledgment_required=True,
    )
    assert result.skip_intervention is False
    assert result.bias_detected == "REVENGE_TRADING"
    assert result.headline == payload["headline"]
    assert result.acknowledgment_required is True  # from the caller, not echoed JSON


async def test_falls_back_gracefully_on_api_error(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")

    class _FakeMessages:
        async def create(self, **kwargs):
            raise RuntimeError("simulated API failure")

    class _FakeAsyncAnthropic:
        def __init__(self, api_key):
            self.messages = _FakeMessages()

    monkeypatch.setattr(genai_client, "AsyncAnthropic", _FakeAsyncAnthropic)

    result = await genai_client.generate_behavioral_intervention(
        order=_fake_order(),
        context={"recent_trades": [], "today_pnl": "0", "minutes_since_last_trade": None},
        signals=[],
        brs=60,
        acknowledgment_required=False,
    )
    assert result.skip_intervention is True
