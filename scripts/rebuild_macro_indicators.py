"""Rebuild public.macro_indicators from public.macro using the new engine.

Non-destructive: idempotent UPSERT (ON CONFLICT DO UPDATE). No DROP, no schema change.
Volatility/ATR/EMA now match db_builder.indicators exactly.
"""
import sys
import time

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")

from db_builder.config import local_engine as _f
from db_builder.macro_indicators import (
    MACRO_INDICATOR_COLUMNS,
    ensure_macro_indicator_indexes,
    recalculate_macro_indicators,
)

SYMBOLS = [
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X",
    "BZ=F", "CL=F", "HK50", "KOR200c1", "CIHc1",
]

eng = _f()

print("=" * 74)
print("REBUILD public.macro_indicators  (idempotent upsert, no DROP)")
print("=" * 74)

t0 = time.time()
ensure_macro_indicator_indexes(eng)

counters = recalculate_macro_indicators(eng, symbols=SYMBOLS)

print()
print("  counters:", counters)
print("  elapsed : {:.1f}s".format(time.time() - t0))

print()
print("=" * 74)
print("POST-REBUILD VALIDATION")
print("=" * 74)

from sqlalchemy import text

with eng.connect() as conn:
    r = conn.execute(text("""
        SELECT COUNT(*),
               COUNT(ma_20), COUNT(rsi_14), COUNT(atr_14), COUNT(volatility_20d),
               COUNT(macd), COUNT(macd_signal), COUNT(volume_ma_20)
        FROM public.macro_indicators
    """)).fetchone()
    print("  rows={} ma20={} rsi={} atr={} vol={} macd={} sig={} vma20={}".format(*r))

    print()
    print("  per-symbol:")
    for row in conn.execute(text("""
        SELECT symbol, COUNT(*), COUNT(atr_14), COUNT(volatility_20d), COUNT(macd_signal)
        FROM public.macro_indicators GROUP BY symbol ORDER BY symbol
    """)).fetchall():
        print("    {:<10} rows={:<4} atr={:<4} vol={:<4} macd_sig={}".format(*row))

    print()
    print("  US10YT=X last 5 rows:")
    for row in conn.execute(text("""
        SELECT date, ma_20, rsi_14, atr_14, volatility_20d, macd, macd_signal, macd_hist
        FROM public.macro_indicators WHERE symbol='US10YT=X'
        ORDER BY date DESC LIMIT 5
    """)).fetchall():
        print("    {} ma20={} rsi={} atr={} vol={} macd={} sig={} hist={}".format(*row))

print()
print("=" * 74)
print("REBUILD COMPLETE")
print("=" * 74)
