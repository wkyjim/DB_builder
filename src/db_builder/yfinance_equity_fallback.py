"""Multi-ticker yfinance fallback for missing Eastmoney session rows."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd
import yfinance as yf
from sqlalchemy import text

from db_builder.config import RAW_TABLE
from db_builder.equity_security_status import filter_core_coverage
from db_builder.trading_calendar import reject_non_trading_dates


YF_BATCH_SIZE = 100
FETCH_BUFFER_DAYS = 5


@dataclass(frozen=True)
class YFinanceFallbackResult:
    frame: pd.DataFrame
    requested: tuple[str, ...]
    found: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class YFinanceRangeFallbackResult:
    frame: pd.DataFrame
    requested_keys: tuple[tuple[str, object], ...]
    found_keys: tuple[tuple[str, object], ...]
    missing_keys: tuple[tuple[str, object], ...]


def fetch_prior_session_universe(
    engine,
    target_date,
    table_name: str = RAW_TABLE,
) -> pd.DataFrame:
    frame = pd.read_sql(
        text(
            f"""
            WITH prior_date AS (
                SELECT MAX(date) AS value
                FROM {table_name}
                WHERE date < :target_date
            )
            SELECT ticker, name, market, close, mkt_cap, pe_ttm
            FROM {table_name}
            CROSS JOIN prior_date
            WHERE date = prior_date.value
              AND ticker IS NOT NULL
            ORDER BY ticker
            """
        ),
        engine,
        params={"target_date": pd.Timestamp(target_date).date()},
    )
    return filter_core_coverage(frame)


def yfinance_symbol(ticker: str, name: str | None = None) -> str:
    ticker = ticker.strip().upper()
    if "_" not in ticker:
        return ticker

    root, suffix = ticker.rsplit("_", 1)
    description = (name or "").lower()
    if suffix == "U":
        return f"{root}-UN"
    if any(token in description for token in ("preferred", " pfd", " series ")):
        return f"{root}-P{suffix}"
    return f"{root}-{suffix}"


def _extract_ticker_frame(batch: pd.DataFrame, yf_ticker: str) -> pd.DataFrame:
    if batch.empty:
        return pd.DataFrame()
    frame = batch.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        level0 = {str(value).upper(): value for value in frame.columns.get_level_values(0).unique()}
        level1 = {str(value).upper(): value for value in frame.columns.get_level_values(1).unique()}
        if yf_ticker.upper() in level0:
            frame = frame[level0[yf_ticker.upper()]].copy()
        elif yf_ticker.upper() in level1:
            frame = frame.xs(level1[yf_ticker.upper()], axis=1, level=1).copy()
        else:
            return pd.DataFrame()
    frame = frame.dropna(how="all").reset_index()
    return frame


def _column(frame: pd.DataFrame, name: str):
    return next((column for column in frame.columns if str(column).lower() == name.lower()), None)


def _number_or_none(value):
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


def _download_batch(
    symbols: list[str],
    target_date,
    *,
    max_retries: int = 5,
) -> pd.DataFrame:
    target = pd.Timestamp(target_date).date()
    start = target - timedelta(days=FETCH_BUFFER_DAYS)
    end = target + timedelta(days=1)
    for attempt in range(1, max_retries + 1):
        try:
            frame = yf.download(
                tickers=" ".join(symbols),
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=False,
                timeout=30,
                repair=False,
            )
            return frame
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(random.uniform(3.0, 8.0))
    return pd.DataFrame()


def _download_batch_range(
    symbols: list[str],
    start_date,
    end_date,
    *,
    max_retries: int = 5,
) -> pd.DataFrame:
    start = pd.Timestamp(start_date).date() - timedelta(days=FETCH_BUFFER_DAYS)
    end = pd.Timestamp(end_date).date() + timedelta(days=1)
    for attempt in range(1, max_retries + 1):
        try:
            return yf.download(
                tickers=" ".join(symbols),
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
                timeout=30,
                repair=False,
            )
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(random.uniform(3.0, 8.0))
    return pd.DataFrame()


def _target_row(
    batch: pd.DataFrame,
    *,
    db_ticker: str,
    yf_ticker: str,
    target_date,
    metadata: dict,
) -> dict | None:
    frame = _extract_ticker_frame(batch, yf_ticker)
    if frame.empty:
        return None

    date_column = _column(frame, "date") or _column(frame, "datetime")
    close_column = _column(frame, "close")
    if date_column is None or close_column is None:
        return None

    frame["_date"] = pd.to_datetime(frame[date_column]).dt.date
    target = pd.Timestamp(target_date).date()
    target_rows = frame[frame["_date"] == target]
    if target_rows.empty:
        return None

    row = target_rows.iloc[-1]
    close = pd.to_numeric(row.get(close_column), errors="coerce")
    if pd.isna(close):
        return None

    prior_rows = frame[frame["_date"] < target].sort_values("_date")
    prior_close = _number_or_none(metadata.get("close"))
    if not prior_rows.empty:
        candidate = pd.to_numeric(prior_rows.iloc[-1].get(close_column), errors="coerce")
        if pd.notna(candidate):
            prior_close = float(candidate)

    def numeric(field: str):
        column = _column(frame, field)
        value = pd.to_numeric(row.get(column), errors="coerce") if column is not None else None
        return None if pd.isna(value) else float(value)

    close = float(close)
    change = close - prior_close if prior_close not in (None, 0) else None
    pct_chg = (change / prior_close) * 100 if change is not None else None
    high = numeric("high")
    low = numeric("low")
    amplitude = (
        ((high - low) / prior_close) * 100
        if high is not None and low is not None and prior_close not in (None, 0)
        else None
    )
    volume = numeric("volume")
    prior_db_close = _number_or_none(metadata.get("close"))
    prior_market_cap = _number_or_none(metadata.get("mkt_cap"))
    prior_pe = _number_or_none(metadata.get("pe_ttm"))
    scale = close / prior_db_close if prior_db_close not in (None, 0) else None

    return {
        "date": target,
        "ticker": db_ticker,
        "name": metadata.get("name") or db_ticker,
        "market": str(metadata.get("market") or "105"),
        "open": numeric("open"),
        "high": high,
        "low": low,
        "close": close,
        "change": change,
        "pct_chg": pct_chg,
        "prev_close": prior_close,
        "turnover": close * volume if volume is not None else None,
        "volume": volume,
        "mkt_cap": prior_market_cap * scale if prior_market_cap is not None and scale else None,
        "ytd_pct_chg": None,
        "pe_ttm": prior_pe * scale if prior_pe is not None and scale else None,
        "amplitude": amplitude,
        "turnover_rate": None,
    }


def fetch_missing_session_rows(
    universe: pd.DataFrame,
    missing_tickers: set[str],
    target_date,
    *,
    batch_size: int = YF_BATCH_SIZE,
    allow_non_trading_day: bool = False,
) -> YFinanceFallbackResult:
    requested = sorted({ticker.upper() for ticker in missing_tickers})
    if not requested:
        return YFinanceFallbackResult(pd.DataFrame(), (), (), ())

    metadata_by_ticker = {
        str(row["ticker"]).upper(): row.to_dict()
        for _, row in universe.iterrows()
        if str(row.get("ticker", "")).upper() in requested
    }
    rows: list[dict] = []
    found: set[str] = set()

    for start in range(0, len(requested), batch_size):
        db_tickers = requested[start:start + batch_size]
        symbol_map = {
            ticker: yfinance_symbol(ticker, metadata_by_ticker.get(ticker, {}).get("name"))
            for ticker in db_tickers
        }
        print(
            f"[YF FALLBACK FETCH] batch={start // batch_size + 1} "
            f"tickers={len(db_tickers)} date={target_date}",
            flush=True,
        )
        batch = _download_batch(list(dict.fromkeys(symbol_map.values())), target_date)
        for ticker in db_tickers:
            result = _target_row(
                batch,
                db_ticker=ticker,
                yf_ticker=symbol_map[ticker],
                target_date=target_date,
                metadata=metadata_by_ticker.get(ticker, {}),
            )
            if result is not None:
                rows.append(result)
                found.add(ticker)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.drop_duplicates(["date", "ticker"], keep="last")
        frame = reject_non_trading_dates(
            frame,
            allow_non_trading_day=allow_non_trading_day,
        )
    missing = sorted(set(requested) - found)
    return YFinanceFallbackResult(
        frame=frame,
        requested=tuple(requested),
        found=tuple(sorted(found)),
        missing=tuple(missing),
    )


def fetch_missing_range_rows(
    universe: pd.DataFrame,
    missing_by_date: dict[object, set[str]],
    *,
    batch_size: int = YF_BATCH_SIZE,
    allow_non_trading_day: bool = False,
) -> YFinanceRangeFallbackResult:
    requested_keys = sorted(
        {
            (str(ticker).upper(), pd.Timestamp(target_date).date())
            for target_date, tickers in missing_by_date.items()
            for ticker in tickers
        },
        key=lambda item: (item[0], item[1]),
    )
    if not requested_keys:
        return YFinanceRangeFallbackResult(pd.DataFrame(), (), (), ())

    requested_dates_by_ticker: dict[str, list[object]] = {}
    for ticker, target_date in requested_keys:
        requested_dates_by_ticker.setdefault(ticker, []).append(target_date)
    metadata_by_ticker = {
        str(row["ticker"]).upper(): row.to_dict()
        for _, row in universe.iterrows()
        if str(row.get("ticker", "")).upper() in requested_dates_by_ticker
    }
    requested_tickers = sorted(requested_dates_by_ticker)
    first_date = min(target_date for _, target_date in requested_keys)
    last_date = max(target_date for _, target_date in requested_keys)
    rows: list[dict] = []
    found_keys: set[tuple[str, object]] = set()

    for start in range(0, len(requested_tickers), batch_size):
        db_tickers = requested_tickers[start:start + batch_size]
        symbol_map = {
            ticker: yfinance_symbol(ticker, metadata_by_ticker.get(ticker, {}).get("name"))
            for ticker in db_tickers
        }
        print(
            f"[YF RANGE FALLBACK] batch={start // batch_size + 1} "
            f"tickers={len(db_tickers)} range={first_date}->{last_date}",
            flush=True,
        )
        batch = _download_batch_range(
            list(dict.fromkeys(symbol_map.values())),
            first_date,
            last_date,
        )
        for ticker in db_tickers:
            for target_date in requested_dates_by_ticker[ticker]:
                result = _target_row(
                    batch,
                    db_ticker=ticker,
                    yf_ticker=symbol_map[ticker],
                    target_date=target_date,
                    metadata=metadata_by_ticker.get(ticker, {}),
                )
                if result is not None:
                    rows.append(result)
                    found_keys.add((ticker, target_date))

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.drop_duplicates(["date", "ticker"], keep="last")
        frame = reject_non_trading_dates(
            frame,
            allow_non_trading_day=allow_non_trading_day,
        )
    missing_keys = sorted(set(requested_keys) - found_keys, key=lambda item: (item[0], item[1]))
    return YFinanceRangeFallbackResult(
        frame=frame,
        requested_keys=tuple(requested_keys),
        found_keys=tuple(sorted(found_keys, key=lambda item: (item[0], item[1]))),
        missing_keys=tuple(missing_keys),
    )
