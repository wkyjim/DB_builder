"""Technical indicator calculation and local indicator updates."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_builder.config import INDICATOR_TABLE, RAW_TABLE
from db_builder.trading_calendar import filter_valid_trading_dates


CHUNK_SIZE = 10000
LOOKBACK_ROWS = 320

INDICATOR_COLUMNS = [
    "ma_5", "ma_20", "ma_50", "ma_100", "ma_200",
    "ema_12", "ema_26",
    "rsi_14",
    "macd", "macd_signal", "macd_hist",
    "atr_14",
    "volume_ma_20", "volume_ratio_20",
    "high_52w", "low_52w",
    "return_5d", "return_20d", "return_60d",
    "volatility_20d",
]


def calculate_group(g: pd.DataFrame) -> pd.DataFrame:
    ticker = g["ticker"].iloc[0]

    g = g.sort_values("date").copy()
    g["ticker"] = ticker

    close = g["close"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    volume = g["volume"].astype(float)

    g["ma_5"] = close.rolling(5).mean()
    g["ma_20"] = close.rolling(20).mean()
    g["ma_50"] = close.rolling(50).mean()
    g["ma_100"] = close.rolling(100).mean()
    g["ma_200"] = close.rolling(200).mean()

    g["ema_12"] = close.ewm(span=12, adjust=False).mean()
    g["ema_26"] = close.ewm(span=26, adjust=False).mean()

    g["macd"] = g["ema_12"] - g["ema_26"]
    g["macd_signal"] = g["macd"].ewm(span=9, adjust=False).mean()
    g["macd_hist"] = g["macd"] - g["macd_signal"]

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    g["rsi_14"] = 100 - (100 / (1 + rs))

    true_range = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    g["atr_14"] = true_range.rolling(14).mean()

    g["volume_ma_20"] = volume.rolling(20).mean()
    g["volume_ratio_20"] = volume / g["volume_ma_20"]
    g["high_52w"] = high.rolling(252).max()
    g["low_52w"] = low.rolling(252).min()
    g["return_5d"] = close.pct_change(5) * 100
    g["return_20d"] = close.pct_change(20) * 100
    g["return_60d"] = close.pct_change(60) * 100
    g["volatility_20d"] = close.pct_change().rolling(20).std() * np.sqrt(252) * 100

    return g[["date", "ticker"] + INDICATOR_COLUMNS]


def bulk_upsert_with_progress(
    engine,
    df: pd.DataFrame,
    chunk_size: int = CHUNK_SIZE,
    indicator_table: str = INDICATOR_TABLE,
) -> None:
    start_time = time.time()
    df = df.reset_index(drop=True)

    required_cols = ["date", "ticker"] + INDICATOR_COLUMNS
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing columns: {missing_cols}")

    df = df[required_cols]
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.where(pd.notnull(df), None)

    records = df.to_dict(orient="records")
    total_records = len(records)
    if total_records == 0:
        print("No rows to upsert.")
        return

    sql = f"""
    INSERT INTO {indicator_table} (
        {", ".join(required_cols)}
    )
    VALUES (
        {", ".join([":" + c for c in required_cols])}
    )
    ON CONFLICT (ticker, date)
    DO UPDATE SET
        {", ".join([f"{c} = EXCLUDED.{c}" for c in INDICATOR_COLUMNS])};
    """

    print(f"Rows to upsert: {total_records:,}")
    print(f"Chunk size: {chunk_size:,}")

    with engine.begin() as conn:
        for i in range(0, total_records, chunk_size):
            chunk = records[i:i + chunk_size]
            conn.execute(text(sql), chunk)

            done = min(i + chunk_size, total_records)
            pct = done / total_records * 100
            elapsed = time.time() - start_time
            print(
                f"Upserted {done:,}/{total_records:,} rows "
                f"({pct:.1f}%) | Elapsed: {elapsed / 60:.1f} min"
            )

    print("Bulk upsert finished.")


def daily_update_missing_indicators(
    engine,
    lookback_rows: int = LOOKBACK_ROWS,
    raw_table: str = RAW_TABLE,
    indicator_table: str = INDICATOR_TABLE,
    chunk_size: int = CHUNK_SIZE,
    dry_run: bool = False,
    limit: int | None = None,
    tickers: list[str] | None = None,
    allow_non_trading_day: bool = False,
) -> None:
    start_time = time.time()
    print("Starting daily update for missing/latest indicator dates")

    ticker_filter = [ticker.strip().upper() for ticker in tickers or [] if ticker.strip()]
    ticker_where = "WHERE ticker = ANY(%(ticker_filter)s)" if ticker_filter else ""
    params = {"ticker_filter": ticker_filter} if ticker_filter else None

    latest_raw = pd.read_sql(
        f"""
        SELECT ticker, MAX(date) AS latest_raw_date
        FROM {raw_table}
        {ticker_where}
        GROUP BY ticker
        """,
        engine,
        params=params,
    )

    latest_ind = pd.read_sql(
        f"""
        SELECT ticker, MAX(date) AS latest_indicator_date
        FROM {indicator_table}
        {ticker_where}
        GROUP BY ticker
        """,
        engine,
        params=params,
    )

    status = latest_raw.merge(latest_ind, on="ticker", how="left")

    if ticker_filter:
        # Explicit ticker runs are small validation runs; recalculate the
        # lookback window even when the latest indicator date is current.
        status_to_update = status.copy()
    else:
        status_to_update = status[
            status["latest_indicator_date"].isna()
            | (status["latest_raw_date"] > status["latest_indicator_date"])
        ].copy()

    if status_to_update.empty:
        print("No tickers need indicator updates.")
        return

    if limit is not None:
        status_to_update = status_to_update.head(limit).copy()

    tickers_to_update = status_to_update["ticker"].tolist()
    print(f"Raw tickers: {len(latest_raw):,}")
    print(f"Tickers needing update: {len(tickers_to_update):,}")
    if dry_run:
        print("[dry-run] indicator rows will be calculated but not upserted")

    df = pd.read_sql(
        f"""
        WITH ranked AS (
            SELECT
                date,
                ticker,
                open,
                high,
                low,
                close,
                volume,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY date DESC
                ) AS rn
            FROM {raw_table}
            WHERE ticker = ANY(%(tickers)s)
        )
        SELECT date, ticker, open, high, low, close, volume
        FROM ranked
        WHERE rn <= %(lookback_rows)s
        ORDER BY ticker, date
        """,
        engine,
        params={"tickers": tickers_to_update, "lookback_rows": lookback_rows},
    )

    if df.empty:
        print("No rows loaded for update.")
        return

    df = filter_valid_trading_dates(
        df,
        allow_non_trading_day=allow_non_trading_day,
    )

    if df.empty:
        print("No valid NYSE session rows loaded for update.")
        return

    latest_indicator_map = dict(
        zip(status_to_update["ticker"], status_to_update["latest_indicator_date"])
    )

    results = []
    grouped = list(df.groupby("ticker", group_keys=False))
    total_tickers = len(grouped)
    print(f"Loaded rows: {len(df):,}")
    print(f"Loaded tickers: {df['ticker'].nunique():,}")

    for idx, (ticker, group) in enumerate(grouped, start=1):
        try:
            result = calculate_group(group).reset_index(drop=True)
            result["ticker"] = ticker

            latest_indicator_date = latest_indicator_map.get(ticker)
            if pd.notnull(latest_indicator_date) and not ticker_filter:
                result = result[result["date"] > latest_indicator_date]

            if not result.empty:
                results.append(result)

            if idx % 25 == 0 or idx == total_tickers:
                queued_rows = sum(len(r) for r in results)
                elapsed = time.time() - start_time
                print(
                    f"[{idx:,}/{total_tickers:,}] Processed {ticker} | "
                    f"Rows queued: {queued_rows:,} | Elapsed: {elapsed / 60:.1f} min"
                )
        except Exception as exc:
            print(f"ERROR calculating {ticker}: {exc}")

    if not results:
        print("No new indicator rows to upsert.")
        return

    result_df = pd.concat(results, ignore_index=True)
    print(f"New indicator rows to upsert: {len(result_df):,}")
    print(f"Tickers updated: {result_df['ticker'].nunique():,}")

    if dry_run:
        preview_cols = ["date", "ticker"] + INDICATOR_COLUMNS[:5]
        print("[dry-run] sample calculated rows:")
        print(result_df[preview_cols].head(10).to_string(index=False))
        print("[dry-run] skipped indicator upsert")
        return

    bulk_upsert_with_progress(engine, result_df, chunk_size, indicator_table)

    elapsed = time.time() - start_time
    print("Daily indicator update complete.")
    print(f"Total elapsed: {elapsed / 60:.1f} minutes")
