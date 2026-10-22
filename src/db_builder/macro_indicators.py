"""Macro indicator engine - mirrors db_builder.indicators exactly.

Every formula is identical to src/db_builder/indicators.py::calculate_group.
The only difference is the key column: `symbol` instead of `ticker`.

Canonical formulas (copied verbatim from indicators.py):
  ma_N          = close.rolling(N).mean()
  ema_N         = close.ewm(span=N, adjust=False).mean()
  macd          = ema_12 - ema_26
  macd_signal   = macd.ewm(span=9, adjust=False).mean()
  macd_hist     = macd - macd_signal
  rsi_14        = 100 - 100/(1 + (gain.rolling(14).mean()/loss.rolling(14).mean()))
  atr_14        = true_range.rolling(14).mean()
  volume_ma_20  = volume.rolling(20).mean()
  volume_ratio  = volume / volume_ma_20
  high_52w/low  = high/low .rolling(252).max()/min()
  return_Nd     = close.pct_change(N) * 100
  volatility_20d= close.pct_change().rolling(20).std() * sqrt(252) * 100

Pandas-native ops are used deliberately: they seed each rolling window from the
first *valid* observation. Hand-rolled loops that seed from a fixed index break
when the leading rows are NULL (the ATR defect).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from sqlalchemy import text

MACRO_RAW_TABLE = "public.macro"
MACRO_INDICATOR_TABLE = "public.macro_indicators"

# Identical set to db_builder.indicators.INDICATOR_COLUMNS by construction.
MACRO_INDICATOR_COLUMNS = [
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

LOOKBACK_ROWS = 320


def calculate_group(g: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators for one symbol. Mirrors indicators.calculate_group."""
    if g.empty:
        return g.reindex(columns=["date", "symbol"] + MACRO_INDICATOR_COLUMNS)

    g = g.sort_values("date").copy()
    g["symbol"] = g["symbol"].iloc[0]

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
    g["volatility_20d"] = (
        close.pct_change().rolling(20).std() * math.sqrt(252) * 100
    )

    return g[["date", "symbol"] + MACRO_INDICATOR_COLUMNS]


def ensure_macro_indicator_indexes(engine, indicator_table: str = MACRO_INDICATOR_TABLE) -> None:
    sql = f"""
    CREATE INDEX IF NOT EXISTS idx_macro_indicators_symbol_date
    ON {indicator_table} (symbol, date DESC);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def recalculate_macro_indicators(
    engine,
    symbols=None,
    start_date=None,
    end_date=None,
    *,
    lookback_rows: int = LOOKBACK_ROWS,
    dry_run: bool = False,
) -> dict:
    """Recompute indicators for macro symbols. Idempotent full-window upsert."""
    counters = {"symbols": 0, "rows_read": 0, "rows_written": 0, "symbols_failed": 0}

    symbol_list = list(symbols) if symbols else None
    where = []
    params: dict = {}
    if symbol_list:
        where.append("symbol = ANY(:symbols)")
        params["symbols"] = symbol_list
    if start_date is not None:
        where.append("date >= :start_date")
        params["start_date"] = str(start_date)
    if end_date is not None:
        where.append("date <= :end_date")
        params["end_date"] = str(end_date)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with engine.connect() as conn:
        rows = pd.read_sql(
            text(f"""
                SELECT symbol, MAX(date) AS latest_date
                FROM {MACRO_RAW_TABLE}
                {where_sql}
                GROUP BY symbol
            """),
            conn,
            params=params,
        )

    required = ["date", "symbol"] + MACRO_INDICATOR_COLUMNS

    for rec in rows.itertuples(index=False):
        symbol = rec.symbol
        latest = rec.latest_date
        if latest is None:
            continue
        try:
            with engine.connect() as conn:
                # Pull enough history for the longest window (252) plus margin.
                hist = pd.read_sql(
                    text("""
                        SELECT date, symbol, close, high, low, volume
                        FROM public.macro
                        WHERE symbol = :s AND date <= :latest
                        ORDER BY date DESC
                        LIMIT :lim
                    """),
                    conn,
                    params={"s": symbol, "latest": latest, "lim": lookback_rows + 260},
                )
            hist = hist.sort_values("date")
            out = calculate_group(hist)
            counters["rows_read"] += len(hist)

            if start_date is not None:
                out = out[out["date"] >= pd.Timestamp(start_date)]
            if end_date is not None:
                out = out[out["date"] <= pd.Timestamp(end_date)]

            out = out.replace([np.inf, -np.inf], np.nan)
            out = out.where(pd.notnull(out), None)
            out = out[required]

            if not dry_run and not out.empty:
                with engine.begin() as conn:
                    conn.execute(
                        text(f"""
                            INSERT INTO {MACRO_INDICATOR_TABLE} (
                                {", ".join(required)}
                            )
                            VALUES (
                                {", ".join([":" + c for c in required])}
                            )
                            ON CONFLICT (symbol, date)
                            DO UPDATE SET
                                {", ".join(f"{c} = EXCLUDED.{c}" for c in MACRO_INDICATOR_COLUMNS)}
                        """),
                        out.to_dict(orient="records"),
                    )
                counters["rows_written"] += len(out)
            counters["symbols"] += 1
        except Exception as exc:
            counters["symbols_failed"] += 1
            print(f"[MACRO INDICATORS] {symbol} failed: {type(exc).__name__}: {exc}")

    return counters
