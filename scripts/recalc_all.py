"""Recalculate technical indicators for the ENTIRE macro universe."""
import importlib
import sys

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
from db_builder.config import local_engine as _f

eng = _f()
mi = importlib.import_module("db_builder.macro_indicators")

print("=" * 88)
print("BEFORE: indicator coverage")
print("=" * 88)
with eng.connect() as conn:
    before = pd.read_sql(text("""
        SELECT COUNT(DISTINCT symbol) AS syms, COUNT(*) AS rows
        FROM public.macro_indicators
    """), conn)
    print("  symbols with indicators: {} | rows: {}".format(
        before["syms"][0], before["rows"][0]))
    tot = conn.execute(text("SELECT COUNT(DISTINCT symbol) FROM public.macro")).scalar()
    print("  symbols in public.macro: {}".format(tot))

print()
print("=" * 88)
print("RECALCULATING (whole universe)")
print("=" * 88)

res = mi.recalculate_macro_indicators(eng)
print("  result:", res)

print()
print("=" * 88)
print("AFTER: indicator coverage")
print("=" * 88)
with eng.connect() as conn:
    after = pd.read_sql(text("""
        SELECT COUNT(DISTINCT symbol) AS syms, COUNT(*) AS rows
        FROM public.macro_indicators
    """), conn)
    print("  symbols with indicators: {} | rows: {}".format(
        after["syms"][0], after["rows"][0]))
    tot = conn.execute(text("SELECT COUNT(DISTINCT symbol) FROM public.macro")).scalar()
    print("  symbols in public.macro: {}".format(tot))

    print()
    print("  --- per-symbol indicator coverage ---")
    df = pd.read_sql(text("""
        SELECT m.symbol,
               COUNT(*) AS price_rows,
               COUNT(i.symbol) AS ind_rows,
               MIN(i.atr_14) FILTER (WHERE i.atr_14 IS NOT NULL) AS atr_min,
               MAX(i.atr_14) AS atr_max,
               MAX(i.volatility_20d) AS vol_max,
               MAX(i.ma_200) AS ma200_max,
               MAX(i.rsi_14) AS rsi_max,
               MAX(i.macd) AS macd_max
        FROM public.macro m
        LEFT JOIN public.macro_indicators i ON i.date = m.date AND i.symbol = m.symbol
        GROUP BY m.symbol ORDER BY m.symbol
    """), conn)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    print(df.to_string(index=False))

    print()
    print("  --- symbols with NO indicators ---")
    none = df[df["ind_rows"] == 0]
    print("  count: {}".format(len(none)))
    if len(none):
        print("  ", list(none["symbol"]))

    print()
    print("  --- symbols with indicators but zero price rows ---")
    zero = df[df["price_rows"] == 0]
    print("  count: {}".format(len(zero)))

    print()
    print("  --- global indicator column population ---")
    cols = ["ma_5", "ma_20", "ma_50", "ma_100", "ma_200", "ema_12", "ema_26",
            "rsi_14", "macd", "macd_signal", "macd_hist", "atr_14",
            "volume_ma_20", "volume_ratio_20", "high_52w", "low_52w",
            "return_5d", "return_20d", "return_60d", "volatility_20d"]
    tot_rows = conn.execute(text("SELECT COUNT(*) FROM public.macro_indicators")).scalar()
    print("  total indicator rows: {}".format(tot_rows))
    print()
    for c in cols:
        n = conn.execute(text(
            'SELECT COUNT(*) FROM public.macro_indicators WHERE "{}" IS NOT NULL'.format(c)
        )).scalar()
        pct = 100.0 * n / tot_rows if tot_rows else 0
        print("    {:<16} {:>7}  {:>5.1f}%".format(c, n, pct))
