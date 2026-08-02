import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.orm import NewsSentimentDaily, PriceHistoryDaily, PriceHistoryMinute

logger = logging.getLogger("mindtrade.loaders")

_DATA_DIR = Path(settings.DATA_DIR)
_DAILY_DIR = _DATA_DIR / "simulation_historical_data"
_MINUTE_DIR = _DATA_DIR / "simulation_price_data_July_1-Aug_30"
_NEWS_DIR = _DATA_DIR / "simulation_news_data_July_1-Aug_30"
_NEWS_FILENAMES = ["simulated_July_news_2026.json", "simulated_August_news_2026.json"]

# asyncpg's hard ceiling on bind parameters per statement (confirmed via
# `asyncpg.exceptions._base.InterfaceError: the number of query arguments
# cannot exceed 32767` -- this is a real protocol limit, not the
# commonly-cited-but-wrong 65535). Chunk size below is computed per table
# from this, since a fixed row count that's safe for a 7-column table
# (price_history_minute) silently isn't for a 10-column one
# (price_history_daily): 5000 rows x 10 columns = 50000 params > 32767.
_MAX_QUERY_PARAMS = 32767


def _ticker_from_daily_filename(path: Path) -> str:
    """6 of 7 files are named "{TICKER}_2026_historical.csv"; AAPL's has an
    extra "simulated_" prefix ("simulated_AAPL_2026_historical.csv").
    """
    stem = path.stem.removeprefix("simulated_")
    return stem.split("_2026_historical")[0].upper()


def _ticker_from_minute_filename(path: Path) -> str:
    """All 7 files are named "simulated_{TICKER}_live.csv"."""
    stem = path.stem.removeprefix("simulated_")
    return stem.split("_live")[0].upper()


def _parse_timestamp_column(col: pd.Series, iso_format: str, dmy_format: str) -> pd.Series:
    """Parse a timestamp column that's consistently in ONE of two formats
    per file: plain ISO (e.g. "2026-01-02" / "2026-06-30 09:30:00") for 6 of
    7 tickers, or "DD-MM-YYYY[ HH:MM]" for GOOG specifically, whose files
    use that format throughout. Tries ISO first, falls back to DD-MM-YYYY;
    raises if neither matches so a genuinely new format doesn't get
    silently misparsed.
    """
    try:
        return pd.to_datetime(col, format=iso_format)
    except ValueError:
        return pd.to_datetime(col, format=dmy_format)


async def _upsert_in_chunks(session: AsyncSession, table, rows: list[dict], index_elements: list[str]) -> None:
    if not rows:
        return
    update_cols = [c for c in rows[0] if c not in index_elements]
    num_columns = len(rows[0])
    chunk_size = max(1, _MAX_QUERY_PARAMS // num_columns)
    try:
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            stmt = pg_insert(table).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={c: getattr(stmt.excluded, c) for c in update_cols},
            )
            await session.execute(stmt)
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def load_historical_daily_data(session: AsyncSession) -> int:
    """Load daily OHLCV bars from data/simulation_historical_data/*.csv into
    price_history_daily. Idempotent: upserts on (ticker, date), safe to re-run.
    """
    if not _DAILY_DIR.is_dir():
        logger.warning("Daily data directory not found: %s -- skipping", _DAILY_DIR)
        return 0

    rows: list[dict] = []
    for csv_path in sorted(_DAILY_DIR.glob("*.csv")):
        ticker = _ticker_from_daily_filename(csv_path)
        if ticker not in settings.TICKERS:
            logger.warning("Skipping %s: derived ticker %r not in configured universe", csv_path.name, ticker)
            continue

        df = pd.read_csv(csv_path)
        # 6 of 7 files use plain ISO "YYYY-MM-DD"; GOOG's file uses
        # "DD-MM-YYYY" throughout instead (verified: no intra-file mixing).
        dates = _parse_timestamp_column(df["timestamp"], "%Y-%m-%d", "%d-%m-%Y").dt.date
        for r, day in zip(df.itertuples(index=False), dates):
            rows.append(
                {
                    "ticker": ticker,
                    "date": day,
                    "open": Decimal(str(r.open)),
                    "high": Decimal(str(r.high)),
                    "low": Decimal(str(r.low)),
                    "close": Decimal(str(r.close)),
                    "adj_close": Decimal(str(r.adjusted_close)),
                    "volume": int(r.volume),
                    "dividend_amount": Decimal(str(r.dividend_amount)),
                    "split_coefficient": Decimal(str(r.split_coefficient)),
                }
            )
        logger.info("Parsed %d daily rows for %s from %s", len(df), ticker, csv_path.name)

    try:
        await _upsert_in_chunks(session, PriceHistoryDaily, rows, index_elements=["ticker", "date"])
    except Exception:
        logger.exception("Failed to load historical daily data")
        raise

    logger.info("Loaded %d daily bars total", len(rows))
    return len(rows)


async def load_intraday_minute_data(session: AsyncSession) -> int:
    """Load minute OHLCV bars from
    data/simulation_price_data_July_1-Aug_30/*_live.csv into
    price_history_minute. Idempotent: upserts on (ticker, timestamp).
    """
    if not _MINUTE_DIR.is_dir():
        logger.warning("Minute data directory not found: %s -- skipping", _MINUTE_DIR)
        return 0

    total = 0
    for csv_path in sorted(_MINUTE_DIR.glob("*_live.csv")):
        ticker = _ticker_from_minute_filename(csv_path)
        if ticker not in settings.TICKERS:
            logger.warning("Skipping %s: derived ticker %r not in configured universe", csv_path.name, ticker)
            continue

        df = pd.read_csv(csv_path)
        # 6 of 7 files use plain ISO "YYYY-MM-DD HH:MM:SS"; GOOG's file uses
        # "DD-MM-YYYY HH:MM" (no seconds) throughout instead. Treated as UTC
        # wall-clock times by the feed simulator (Sprint 4).
        timestamps = _parse_timestamp_column(
            df["timestamp"], "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M"
        ).dt.tz_localize("UTC")
        rows = [
            {
                "ticker": ticker,
                "timestamp": ts.to_pydatetime(),
                "open": Decimal(str(r.open)),
                "high": Decimal(str(r.high)),
                "low": Decimal(str(r.low)),
                "close": Decimal(str(r.close)),
                "volume": int(r.volume),
            }
            for r, ts in zip(df.itertuples(index=False), timestamps)
        ]

        try:
            await _upsert_in_chunks(session, PriceHistoryMinute, rows, index_elements=["ticker", "timestamp"])
        except Exception:
            logger.exception("Failed to load minute data for %s", ticker)
            raise

        logger.info("Loaded %d minute bars for %s from %s", len(rows), ticker, csv_path.name)
        total += len(rows)

    logger.info("Loaded %d minute bars total", total)
    return total


async def load_sentiment_data(session: AsyncSession) -> int:
    """Load news sentiment from the two raw news JSON files in
    data/simulation_news_data_July_1-Aug_30/ into news_sentiment_daily.

    Each file is a raw multi-ticker news feed (~2,667 distinct tickers,
    plus CRYPTO:*/FOREX:* pseudo-tickers) keyed by date as "YYYYMMDD" ->
    list of articles, each with a `ticker_sentiment` list. This filters
    every article's ticker_sentiment down to our configured universe, then
    aggregates ticker_sentiment_score into a daily mean per (ticker, date)
    and counts matching mentions into headline_count.

    Idempotent: upserts on (ticker, date), safe to re-run.
    """
    if not _NEWS_DIR.is_dir():
        logger.warning("News data directory not found: %s -- skipping", _NEWS_DIR)
        return 0

    universe = set(settings.TICKERS)
    sums: dict[tuple[str, object], float] = {}
    counts: dict[tuple[str, object], int] = {}

    for filename in _NEWS_FILENAMES:
        path = _NEWS_DIR / filename
        if not path.is_file():
            logger.warning("News file not found: %s -- skipping", path)
            continue

        with path.open(encoding="utf-8") as f:
            data = json.load(f)

        articles_scanned = 0
        for date_key, articles in data.items():
            day = datetime.strptime(date_key, "%Y%m%d").date()
            for article in articles:
                articles_scanned += 1
                for ts in article.get("ticker_sentiment", []):
                    ticker = ts.get("ticker")
                    if ticker not in universe:
                        continue
                    score = float(ts["ticker_sentiment_score"])
                    key = (ticker, day)
                    sums[key] = sums.get(key, 0.0) + score
                    counts[key] = counts.get(key, 0) + 1

        logger.info("Parsed %s (%d articles scanned)", filename, articles_scanned)

    rows = [
        {
            "ticker": ticker,
            "date": day,
            "avg_sentiment": Decimal(str(round(sums[(ticker, day)] / counts[(ticker, day)], 4))),
            "headline_count": counts[(ticker, day)],
        }
        for ticker, day in sums
    ]

    try:
        await _upsert_in_chunks(session, NewsSentimentDaily, rows, index_elements=["ticker", "date"])
    except Exception:
        logger.exception("Failed to load news sentiment data")
        raise

    logger.info("Loaded sentiment for %d (ticker, date) pairs", len(rows))
    return len(rows)
