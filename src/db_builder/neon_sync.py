"""Local PostgreSQL to Neon sync helpers."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_builder.config import INDICATOR_TABLE, RAW_TABLE
from db_builder.trading_calendar import filter_valid_trading_dates


CHUNK_SIZE = 5000
OVERLAP_DAYS = 5

RAW_NUMERIC_COLUMNS = [
    "open", "high", "low", "close", "change", "pct_chg", "prev_close",
    "turnover", "volume", "mkt_cap", "ytd_pct_chg", "pe_ttm",
    "amplitude", "turnover_rate",
]

INDICATOR_NUMERIC_COLUMNS = [
    "ma_5", "ma_20", "ma_50", "ma_100", "ma_200",
    "ema_12", "ema_26", "rsi_14",
    "macd", "macd_signal", "macd_hist",
    "atr_14",
    "volume_ma_20", "volume_ratio_20",
    "high_52w", "low_52w",
    "return_5d", "return_20d", "return_60d",
    "volatility_20d",
]


def setup_neon_required_objects(neon_engine) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS public.sync_state (
        table_name text PRIMARY KEY,
        latest_date date,
        updated_at timestamp DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_us_equities_ticker_date
    ON public.us_equities (ticker, date);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_us_equities_indicators_ticker_date
    ON public.us_equities_indicators (ticker, date);
    """

    with neon_engine.begin() as conn:
        conn.execute(text(sql))

    print("Neon sync_state and indexes ready.")


def get_sync_latest_date(neon_engine, table_name: str):
    sql = """
    SELECT latest_date
    FROM public.sync_state
    WHERE table_name = :table_name
    """

    df = pd.read_sql(text(sql), neon_engine, params={"table_name": table_name})
    if df.empty or pd.isna(df["latest_date"].iloc[0]):
        raise ValueError(
            f"No sync_state found for {table_name}. "
            "Run the first-time custom-date upload first."
        )

    return df["latest_date"].iloc[0]


def update_sync_state(neon_engine, table_name: str, latest_date) -> None:
    sql = """
    INSERT INTO public.sync_state (
        table_name,
        latest_date,
        updated_at
    )
    VALUES (
        :table_name,
        :latest_date,
        now()
    )
    ON CONFLICT (table_name)
    DO UPDATE SET
        latest_date = EXCLUDED.latest_date,
        updated_at = now();
    """

    with neon_engine.begin() as conn:
        conn.execute(
            text(sql),
            {"table_name": table_name, "latest_date": latest_date},
        )


def fetch_local_rows_for_daily_sync(
    local_engine,
    table_name: str,
    latest_date,
    overlap_days: int = OVERLAP_DAYS,
    limit: int | None = None,
    tickers: list[str] | None = None,
) -> pd.DataFrame:
    limit_clause = "LIMIT %(limit)s" if limit is not None else ""
    ticker_clause = "AND ticker = ANY(%(tickers)s)" if tickers else ""
    sql = f"""
    SELECT *
    FROM {table_name}
    WHERE date >= (%(latest_date)s::date - (%(overlap_days)s * INTERVAL '1 day'))
    {ticker_clause}
    ORDER BY date, ticker
    {limit_clause}
    """

    params = {"latest_date": latest_date, "overlap_days": overlap_days}
    if limit is not None:
        params["limit"] = limit
    if tickers:
        params["tickers"] = [ticker.strip().upper() for ticker in tickers if ticker.strip()]

    df = pd.read_sql(
        sql,
        local_engine,
        params=params,
    )

    print(
        f"{table_name}: fetched {len(df):,} rows "
        f"from local where date >= {latest_date} - {overlap_days} days"
    )

    return df


def clean_dataframe_for_target(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
    df = df.copy()

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    numeric_cols = (
        RAW_NUMERIC_COLUMNS
        if table_name == RAW_TABLE
        else INDICATOR_NUMERIC_COLUMNS
    )

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["ticker", "name", "market"]:
        if col in df.columns:
            df[col] = df[col].astype("string")

    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.where(pd.notnull(df), None)

    return df


def bulk_temp_staging_upsert_to_neon(
    neon_engine,
    df: pd.DataFrame,
    target_table: str,
    conflict_cols: list[str],
    chunk_size: int = CHUNK_SIZE,
):
    if df.empty:
        print(f"No rows to upload for {target_table}.")
        return None

    start_time = time.time()
    df = clean_dataframe_for_target(df, target_table)

    columns = df.columns.tolist()
    update_cols = [c for c in columns if c not in conflict_cols]
    temp_table = f"temp_{target_table.split('.')[-1]}"
    column_list = ", ".join(columns)

    merge_sql = f"""
    INSERT INTO {target_table} (
        {column_list}
    )
    SELECT
        {column_list}
    FROM {temp_table}
    ON CONFLICT ({", ".join(conflict_cols)})
    DO UPDATE SET
        {", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])};
    """

    print(f"Bulk uploading {len(df):,} rows to {target_table}")
    print("Using TEMP staging table with same schema as target.")

    with neon_engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TEMP TABLE {temp_table}
            (LIKE {target_table} INCLUDING DEFAULTS)
            ON COMMIT DROP;
        """))

        df.to_sql(
            name=temp_table,
            con=conn,
            if_exists="append",
            index=False,
            chunksize=chunk_size,
            method="multi",
        )

        conn.execute(text(merge_sql))

    latest_uploaded_date = df["date"].max()
    elapsed = time.time() - start_time

    print(f"Finished bulk temp-staging upsert to {target_table}")
    print(f"Latest uploaded date: {latest_uploaded_date}")
    print(f"Elapsed: {elapsed / 60:.1f} min")

    return latest_uploaded_date


def daily_bulk_sync_to_neon(
    local_engine,
    neon_engine,
    tables: list[str] | None = None,
    overlap_days: int = OVERLAP_DAYS,
    chunk_size: int = CHUNK_SIZE,
    dry_run: bool = False,
    limit: int | None = None,
    tickers: list[str] | None = None,
    allow_non_trading_day: bool = False,
) -> None:
    start_time = time.time()
    tables = tables or [RAW_TABLE, INDICATOR_TABLE]
    ticker_filter = [ticker.strip().upper() for ticker in tickers or [] if ticker.strip()]

    print("Starting daily bulk sync to Neon")
    if dry_run:
        print("[dry-run] skipping Neon setup, upload, and sync_state updates")
    else:
        setup_neon_required_objects(neon_engine)

    for table_name in tables:
        print(f"Processing {table_name}")

        latest_date = get_sync_latest_date(neon_engine, table_name)
        print(f"sync_state latest date for {table_name}: {latest_date}")

        df = fetch_local_rows_for_daily_sync(
            local_engine=local_engine,
            table_name=table_name,
            latest_date=latest_date,
            overlap_days=overlap_days,
            limit=limit,
            tickers=ticker_filter,
        )
        df = filter_valid_trading_dates(
            df,
            allow_non_trading_day=allow_non_trading_day,
        )

        if dry_run:
            cleaned = clean_dataframe_for_target(df, table_name)
            print(
                f"[dry-run] {table_name}: would upload {len(cleaned):,} rows "
                f"with chunk_size={chunk_size:,}"
            )
            if not cleaned.empty:
                preview_cols = [c for c in ["date", "ticker", "close"] if c in cleaned.columns]
                print(cleaned[preview_cols].head(10).to_string(index=False))
            continue

        latest_uploaded_date = bulk_temp_staging_upsert_to_neon(
            neon_engine=neon_engine,
            df=df,
            target_table=table_name,
            conflict_cols=["ticker", "date"],
            chunk_size=chunk_size,
        )

        if latest_uploaded_date is not None and not ticker_filter:
            update_sync_state(neon_engine, table_name, latest_uploaded_date)
            print(f"sync_state updated: {table_name} = {latest_uploaded_date}")
        elif latest_uploaded_date is not None:
            print(
                f"Ticker-scoped sync uploaded through {latest_uploaded_date}; "
                "global sync_state was not advanced."
            )

    elapsed = time.time() - start_time
    print("Daily bulk sync to Neon complete.")
    print(f"Total elapsed: {elapsed / 60:.1f} min")
