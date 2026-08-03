from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import PriceHistoryDaily, PriceHistoryMinute

# Task 6.6 (MASTER_BUILD_PLAN Sprint 6): pure pandas, no GenAI involved.
_DEFAULT_SMA_EMA_WINDOW = 20
_RSI_WINDOW = 14
_MACD_FAST, _MACD_SLOW, _MACD_SIGNAL = 12, 26, 9
_BB_WINDOW = 20
_BB_NUM_STD = 2.0


async def _load_closes(db: AsyncSession, ticker: str, timeframe: str) -> pd.Series:
    if timeframe == "1d":
        stmt = (
            select(PriceHistoryDaily.date, PriceHistoryDaily.close)
            .where(PriceHistoryDaily.ticker == ticker)
            .order_by(PriceHistoryDaily.date.asc())
        )
    else:
        stmt = (
            select(PriceHistoryMinute.timestamp, PriceHistoryMinute.close)
            .where(PriceHistoryMinute.ticker == ticker)
            .order_by(PriceHistoryMinute.timestamp.asc())
        )
    rows = (await db.execute(stmt)).all()
    return pd.Series([float(r[1]) for r in rows], index=[r[0] for r in rows], dtype="float64")


def sma(closes: pd.Series, window: int = _DEFAULT_SMA_EMA_WINDOW) -> pd.Series:
    return closes.rolling(window=window).mean()


def ema(closes: pd.Series, window: int = _DEFAULT_SMA_EMA_WINDOW) -> pd.Series:
    return closes.ewm(span=window, adjust=False).mean()


def rsi(closes: pd.Series, window: int = _RSI_WINDOW) -> pd.Series:
    """Wilder's RSI: average gain/loss smoothed with Wilder's alpha
    (1/window), not a plain SMA -- this is what distinguishes "Wilder's
    RSI" from a naive RSI implementation.
    """
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: pd.Series, fast: int = _MACD_FAST, slow: int = _MACD_SLOW, signal: int = _MACD_SIGNAL
) -> dict[str, pd.Series]:
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return {"macd": macd_line, "signal": signal_line, "histogram": macd_line - signal_line}


def bollinger_bands(closes: pd.Series, window: int = _BB_WINDOW, num_std: float = _BB_NUM_STD) -> dict[str, pd.Series]:
    middle = closes.rolling(window=window).mean()
    std = closes.rolling(window=window).std()
    return {"upper": middle + num_std * std, "middle": middle, "lower": middle - num_std * std}


def _to_list(series: pd.Series) -> list[Optional[float]]:
    return [None if pd.isna(v) else round(float(v), 4) for v in series]


async def compute_indicators(db: AsyncSession, ticker: str, timeframe: str) -> dict:
    """Returns SMA/EMA/RSI/MACD/Bollinger Bands over the ticker's full
    loaded price history for the requested timeframe ("1d" for daily bars,
    anything else for minute bars). Each series is None-padded at the
    front where its window hasn't filled yet, aligned 1:1 with
    "timestamps" so a frontend can zip them straight onto a chart.
    """
    closes = await _load_closes(db, ticker, timeframe)
    if closes.empty:
        return {
            "timestamps": [], "sma": [], "ema": [], "rsi": [],
            "macd": {"macd": [], "signal": [], "histogram": []},
            "bb": {"upper": [], "middle": [], "lower": []},
        }

    macd_result = macd(closes)
    bb_result = bollinger_bands(closes)

    return {
        "timestamps": [t.isoformat() if hasattr(t, "isoformat") else str(t) for t in closes.index],
        "sma": _to_list(sma(closes)),
        "ema": _to_list(ema(closes)),
        "rsi": _to_list(rsi(closes)),
        "macd": {
            "macd": _to_list(macd_result["macd"]),
            "signal": _to_list(macd_result["signal"]),
            "histogram": _to_list(macd_result["histogram"]),
        },
        "bb": {
            "upper": _to_list(bb_result["upper"]),
            "middle": _to_list(bb_result["middle"]),
            "lower": _to_list(bb_result["lower"]),
        },
    }
