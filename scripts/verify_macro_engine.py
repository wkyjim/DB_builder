"""Verify the new macro indicator engine against the canonical equities engine.

Goal: prove the macro engine is a faithful copy of db_builder.indicators,
then report what the current production macro_indicators values would change to.
Read-only against production.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")

from db_builder.config import local_engine as _f
from db_builder.indicators import INDICATOR_COLUMNS, calculate_group as calc_equities
from db_builder.macro_indicators import (
    MACRO_INDICATOR_COLUMNS,
    calculate_group as calc_macro,
)

eng = _f()

print("=" * 74)
print("STEP 1 - PARITY: macro engine vs equities engine on identical data")
print("=" * 74)

n = 500
dates = pd.bdate_range("2025-01-02", periods=n)
close = pd.Series(np.linspace(100, 130, n) + np.sin(np.arange(n) / 6.0), index=dates)
high = close + np.abs(np.sin(np.arange(n) / 3.0))
low = close - np.abs(np.cos(np.arange(n) / 5.0))

eq = calc_equities(pd.DataFrame({
    "date": dates, "ticker": "T", "close": close.values,
    "high": high.values, "low": low.values,
    "volume": (np.arange(n) + 1.0) * 100,
}))
mc = calc_macro(pd.DataFrame({
    "date": dates, "symbol": "T", "close": close.values,
    "high": high.values, "low": low.values,
    "volume": (np.arange(n) + 1.0) * 100,
}))

print("  columns identical:", MACRO_INDICATOR_COLUMNS == INDICATOR_COLUMNS)

mismatch = {}
for c in INDICATOR_COLUMNS:
    a, b = eq[c].to_numpy(dtype=float), mc[c].to_numpy(dtype=float)
    both_nan = np.isnan(a) & np.isnan(b)
    diff = np.abs(a - b)
    worst = np.nanmax(np.where(both_nan, 0.0, diff))
    if worst > 1e-9:
        mismatch[c] = worst
print("  column mismatches:", mismatch if mismatch else "NONE - exact parity")

print()
print("=" * 74)
print("STEP 2 - CURRENT PRODUCTION macro_indicators vs NEW engine (US10YT=X)")
print("=" * 74)

from sqlalchemy import text

with eng.connect() as conn:
    stored = pd.read_sql(text("""
        SELECT date, ma_5, ma_20, ma_50, ma_100, ma_200, ema_12, ema_26, rsi_14,
               atr_14, volume_ma_20, volume_ratio_20, high_52w, low_52w,
               return_5d, return_20d, return_60d, volatility_20d,
               macd, macd_signal, macd_hist
        FROM public.macro_indicators WHERE symbol='US10YT=X' ORDER BY date
    """), conn)

    raw = pd.read_sql(text("""
        SELECT date, symbol, close, high, low, volume FROM public.macro
        WHERE symbol='US10YT=X' ORDER BY date DESC LIMIT 580
    """), conn).sort_values("date")

fresh = calc_macro(raw)
m = stored.merge(fresh, on="date", suffixes=("_old", "_new"))

print("  stored rows={} | recomputed rows={}".format(len(stored), len(fresh)))
print()
print("  {:<16} {:>8} {:>8} {:>12}".format("column", "changed", "null_old", "null_new"))
print("  " + "-" * 48)
for c in MACRO_INDICATOR_COLUMNS:
    o, nw = m[f"{c}_old"], m[f"{c}_new"]
    changed = int(((o - nw).abs() > 0.01).sum())
    print("  {:<16} {:>8} {:>8} {:>12}".format(
        c, changed, int(o.isna().sum()), int(nw.isna().sum())))

print()
print("=" * 74)
print("STEP 3 - KEY VALUES (last row)")
print("=" * 74)
last_o = stored.iloc[-1]
last_n = fresh.iloc[-1]
print("  {:<16} {:>14} {:>14}".format("column", "stored", "new"))
print("  " + "-" * 46)
for c in ["atr_14", "volatility_20d", "ma_20", "rsi_14", "macd", "macd_signal"]:
    print("  {:<16} {:>14} {:>14}".format(
        c,
        "NULL" if pd.isna(last_o[c]) else f"{last_o[c]:.4f}",
        "NULL" if pd.isna(last_n[c]) else f"{last_n[c]:.4f}",
    ))

print()
print("=" * 74)
print("VERIFY COMPLETE")
print("=" * 74)
