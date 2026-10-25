"""Mirror public.macro_indicators from local PostgreSQL to Neon.

Local remains the system of record. Neon carries the rolling window that the
API/dashboard read path needs, and this module is the only writer to the Neon
copy. The schema is kept identical to the local table so the two never drift.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

MACRO_INDICATOR_TABLE = "public.macro_indicators"
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "20261025_macro_indicators.sql"

# Columns written to Neon, in table order. Everything except the primary key.
INDICATOR_COLUMNS = [
    "ma_5", "ma_20", "ma_50", "ma_100", "ma_200",
    "ema_12", "ema_26", "rsi_14", "atr_14",
    "volume_ma_20", "volume_ratio_20",
    "high_52w", "low_52w",
    "return_5d", "return_20d", "return_60d", "volatility_20d",
    "macd", "macd_signal", "macd_hist",
]


def setup_macro_indicators_schema(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(MIGRATION.read_text(encoding="utf-8"))


def _target_columns(engine) -> list[str]:
    query = text(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'macro_indicators'
        ORDER BY ordinal_position
        """
    )
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(query)]


def select_sql(since: str | None = None) -> tuple[str, dict]:
    """SQL for reading the local indicator rows to mirror, plus its params."""
    sql = "SELECT symbol, date, " + ", ".join(INDICATOR_COLUMNS) + " FROM " + MACRO_INDICATOR_TABLE
    params: dict = {}
    if since is not None:
        sql += " WHERE date >= :since"
        params["since"] = str(since)
    return sql + " ORDER BY symbol, date", params


def count_sql(since: str | None = None) -> tuple[str, dict]:
    """SQL for counting the rows a window currently holds."""
    sql = f"SELECT count(*) FROM {MACRO_INDICATOR_TABLE}"
    params: dict = {}
    if since is not None:
        sql += " WHERE date >= :since"
        params["since"] = str(since)
    return sql, params


def fetch_macro_indicators(engine, *, since=None) -> pd.DataFrame:
    """Read the local indicator rows to mirror. Ordered for deterministic upserts."""
    sql, params = select_sql(since)
    return pd.read_sql(text(sql), engine, params=params)


def bulk_upsert_macro_indicators(engine, frame: pd.DataFrame, *, chunk_size: int = 500) -> int:
    """Idempotent upsert keyed on (symbol, date). Uses a temp staging table."""
    if frame.empty:
        return 0
    setup_macro_indicators_schema(engine)
    columns = _target_columns(engine)
    prepared = frame.reindex(columns=columns).replace([np.inf, -np.inf], np.nan)
    prepared = prepared.astype(object).where(pd.notna(prepared), None)
    # numeric columns round-trip through the text protocol as strings; make sure
    # NaN never reaches the wire as the literal.
    for column in INDICATOR_COLUMNS:
        if column in prepared.columns:
            prepared[column] = prepared[column].map(
                lambda value: None if value is None or (isinstance(value, float) and not math.isfinite(value)) else value
            )
    with engine.begin() as connection:
        connection.execute(text(
            f"CREATE TEMP TABLE temp_macro_indicators (LIKE {MACRO_INDICATOR_TABLE} INCLUDING DEFAULTS) ON COMMIT DROP"
        ))
        prepared.to_sql(
            "temp_macro_indicators", connection, if_exists="append", index=False,
            chunksize=chunk_size, method="multi",
        )
        connection.execute(text(
            f"""
            INSERT INTO {MACRO_INDICATOR_TABLE} ({', '.join(columns)})
            SELECT {', '.join(columns)} FROM temp_macro_indicators
            ON CONFLICT (symbol, date) DO UPDATE SET
              {', '.join(f'{column} = EXCLUDED.{column}' for column in INDICATOR_COLUMNS)}
            """
        ))
    return len(prepared)


def count_macro_indicators(engine, *, since=None) -> int:
    sql, params = count_sql(since)
    with engine.connect() as connection:
        return int(connection.execute(text(sql), params).scalar_one())


def sync_macro_indicators_to_neon(
    local_engine,
    neon_engine,
    *,
    since=None,
    chunk_size: int = 500,
    retries: int = 3,
) -> dict:
    """Mirror local macro indicators into Neon.

    Idempotent: reruns re-send the same window. `since` bounds the window for a
    one-off backfill; the hourly run passes None and gets the full mirror.
    """
    setup_macro_indicators_schema(local_engine)
    setup_macro_indicators_schema(neon_engine)

    local = fetch_macro_indicators(local_engine, since=since)
    if local.empty:
        return {"local_rows": 0, "uploaded": 0, "failed": 0, "since": since}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[UPSERT NEON MACRO INDICATORS] rows={len(local):,} "
                  f"since={since or 'all'} attempt={attempt}/{retries}")
            uploaded = bulk_upsert_macro_indicators(neon_engine, local, chunk_size=chunk_size)
            remote = count_macro_indicators(neon_engine, since=since)
            if since is not None and remote < len(local):
                raise RuntimeError(
                    f"Neon validation: expected >= {len(local)} rows since {since}, found {remote}"
                )
            return {
                "local_rows": len(local),
                "uploaded": uploaded,
                "remote_rows": remote,
                "failed": 0,
                "since": str(since) if since else None,
            }
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError("macro_indicators Neon sync failed") from last_error
