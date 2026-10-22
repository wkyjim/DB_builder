"""(1) actual macro_indicators columns
   (2) verify the two new symbols
"""
import sys

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts")

from db_builder import config
from db_builder.config import local_engine
from macro_data_fetch import OILPRICEAPI_ASSETS
from sqlalchemy import text
import pandas as pd

eng = local_engine()
syms = sorted(OILPRICEAPI_ASSETS)

with eng.connect() as c:
    cols = [r[0] for r in c.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='macro_indicators' "
        "ORDER BY ordinal_position")).fetchall()]
    print("=" * 92)
    print("macro_indicators columns")
    print("=" * 92)
    print("  " + ", ".join(cols))

    print()
    print("=" * 92)
    print("stored indicators for the new symbols")
    print("=" * 92)
    for s in syms:
        df = pd.read_sql(text(
            "SELECT count(*) n, min(date) mn, max(date) mx, "
            "       max(ma_20) ma20, max(volume_ma_20) vma20, "
            "       max(ma_50) ma50 "
            "  FROM public.macro_indicators WHERE symbol=:s"), c, params={"s": s})
        r = df.iloc[0]
        print("  {:<16} rows={:<5} {} .. {}   max_ma20={}  max_ma50={}  max_vma20={}".format(
            s, r["n"], r["mn"], r["mx"], r["ma20"], r["ma50"], r["vma20"]))

    print()
    print("=" * 92)
    print("recent rows sample")
    print("=" * 92)
    for s in syms:
        df = pd.read_sql(text(
            "SELECT date, close, ma_20, ma_50, rsi_14 "
            "  FROM public.macro_indicators WHERE symbol=:s ORDER BY date DESC LIMIT 3"),
            c, params={"s": s})
        print()
        print("  {} (latest 3)".format(s))
        print(df.to_string(index=False))

    print()
    print("=" * 92)
    print("NULL counts after warm-up (date > 2025-11-20)")
    print("=" * 92)
    df = pd.read_sql(text(
        "SELECT symbol, "
        "       count(*) FILTER (WHERE ma_20 IS NULL) ma20_null, "
        "       count(*) FILTER (WHERE ma_50 IS NULL) ma50_null, "
        "       count(*) FILTER (WHERE rsi_14 IS NULL) rsi_null, "
        "       count(*) total "
        "  FROM public.macro_indicators WHERE symbol = ANY(:s) AND date > '2025-11-20' "
        " GROUP BY symbol"), c, params={"s": syms})
    print(df.to_string(index=False) if len(df) else "  none")
