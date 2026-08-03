"""Unit tests for indicators.py: SMA, EMA, Wilder's RSI, MACD, and
Bollinger Bands against known/hand-computable reference sequences, plus
compute_indicators' end-to-end shape against real seeded price data.
"""

import pandas as pd
import pytest

from app.services import indicators

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_sma_matches_manual_calculation():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    result = indicators.sma(closes, window=3)
    assert pd.isna(result.iloc[0])
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == pytest.approx((1 + 2 + 3) / 3)
    assert result.iloc[-1] == pytest.approx((8 + 9 + 10) / 3)


async def test_ema_seeds_on_first_observation():
    # ewm(adjust=False) seeds the recursive average on the first data
    # point -- this is the property that distinguishes it from a plain
    # windowed average, which is what makes it "exponential".
    closes = pd.Series([10.0, 20.0, 30.0])
    result = indicators.ema(closes, window=2)
    assert result.iloc[0] == pytest.approx(10.0)


async def test_rsi_approaches_100_for_strict_uptrend():
    closes = pd.Series([float(i) for i in range(1, 40)])  # no losses at all
    result = indicators.rsi(closes, window=14)
    assert result.iloc[-1] == pytest.approx(100.0, abs=0.01)


async def test_rsi_approaches_0_for_strict_downtrend():
    closes = pd.Series([float(i) for i in range(40, 1, -1)])  # no gains at all
    result = indicators.rsi(closes, window=14)
    assert result.iloc[-1] == pytest.approx(0.0, abs=0.01)


async def test_macd_positive_for_sustained_uptrend():
    # In a sustained uptrend the fast EMA pulls ahead of the slow EMA, so
    # the MACD line (fast - slow) should be positive.
    closes = pd.Series([float(i) for i in range(1, 60)])
    result = indicators.macd(closes)
    assert result["macd"].iloc[-1] > 0


async def test_bollinger_bands_ordering_and_middle_equals_sma():
    closes = pd.Series(
        [10, 12, 9, 15, 11, 14, 10, 13, 12, 16, 11, 15, 10, 14, 12, 17, 13, 16, 12, 15, 14], dtype="float64"
    )
    result = indicators.bollinger_bands(closes, window=10)
    last = closes.index[-1]
    assert result["upper"].loc[last] > result["middle"].loc[last] > result["lower"].loc[last]
    assert result["middle"].equals(indicators.sma(closes, window=10))


async def test_compute_indicators_unknown_ticker_returns_empty(db):
    result = await indicators.compute_indicators(db, "ZZZZ_NOPE", "1d")
    assert result == {
        "timestamps": [], "sma": [], "ema": [], "rsi": [],
        "macd": {"macd": [], "signal": [], "histogram": []},
        "bb": {"upper": [], "middle": [], "lower": []},
    }


async def test_compute_indicators_real_ticker_series_are_aligned(db):
    result = await indicators.compute_indicators(db, "AAPL", "1d")
    n = len(result["timestamps"])
    assert n > 0
    assert len(result["sma"]) == n
    assert len(result["ema"]) == n
    assert len(result["rsi"]) == n
    assert len(result["macd"]["macd"]) == n
    assert len(result["macd"]["signal"]) == n
    assert len(result["macd"]["histogram"]) == n
    assert len(result["bb"]["upper"]) == n
    assert len(result["bb"]["middle"]) == n
    assert len(result["bb"]["lower"]) == n
