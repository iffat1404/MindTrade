"""Unit tests for genai_analyst.generate_moment_analysis: graceful
degradation when no API key is configured, and the happy/failure paths
with the Anthropic client mocked out (same convention as
test_genai_client.py -- no real API key is available in this environment).
"""

import json
import types
from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.services import genai_analyst

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_moment(**kw):
    defaults = dict(
        kind="highest_brs",
        display_type="caution",
        behavioral_score_id=None,
        order_id=None,
        timestamp=datetime(2026, 7, 1, 9, 37, tzinfo=timezone.utc),
        ticker="AAPL",
        side="BUY",
        bias_name="REVENGE_TRADING",
        brs=72,
        intervention_shown=True,
        trader_proceeded=True,
        realized_pnl=None,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


async def test_skips_analysis_when_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    result = await genai_analyst.generate_moment_analysis(_fake_moment())
    assert result.skip is True
    assert result.analysis is None
    assert result.lesson is None


async def test_generates_analysis_on_mocked_success(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")

    payload = {
        "analysis": "You bought right after a loss on the same ticker -- classic revenge trading.",
        "lesson": "Wait 5 minutes and reassess before re-entering after a loss.",
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

    monkeypatch.setattr(genai_analyst, "AsyncAnthropic", _FakeAsyncAnthropic)

    result = await genai_analyst.generate_moment_analysis(_fake_moment())
    assert result.skip is False
    assert result.analysis == payload["analysis"]
    assert result.lesson == payload["lesson"]


async def test_falls_back_gracefully_on_api_error(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")

    class _FakeMessages:
        async def create(self, **kwargs):
            raise RuntimeError("simulated API failure")

    class _FakeAsyncAnthropic:
        def __init__(self, api_key):
            self.messages = _FakeMessages()

    monkeypatch.setattr(genai_analyst, "AsyncAnthropic", _FakeAsyncAnthropic)

    result = await genai_analyst.generate_moment_analysis(_fake_moment())
    assert result.skip is True
