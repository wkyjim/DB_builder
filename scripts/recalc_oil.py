"""Recalculate indicators for the two new OilPriceAPI symbols."""
import sys

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts")

from db_builder import config
from db_builder.config import local_engine
from db_builder import macro_indicators as mi
from macro_data_fetch import OILPRICEAPI_ASSETS
from sqlalchemy import text
import pandas as pd

eng = local_engine()
syms = sorted(OILPRICEAPI_ASSETS)
print("recalculating: {}".format(syms))
res = mi.recalculate_macro_indicators(eng, symbols=syms)
print()
print("=" * 92)
print("RESULT")
print("=" * 92)
for k, v in res.items():
    print("  {:<28} {}".format(k, v))

print()
print("=" * 92)
print("stored indicators")
print("=" * 92)
with eng.connect() as c:
    for s in syms:
        df = pd.read_sql(text(
            "SELECT count(*) n, min(date) mn, max(date) mx, "
            "       max(sma_20) sma20, max(volume_ma_20) vma20 "
            "  FROM public.macro_indicators WHERE symbol=:s"), c, params={"s": s})
        r = df.iloc[0]
        print("  {:<16} rows={:<5} {} .. {}   max_sma20={}  max_volume_ma20={}".format(
            s, r["n"], r["mn"], r["mx"], r["sma20"], r["vma20"]))
    print()
    nulls = pd.read_sql(text(
        "SELECT symbol, count(*) n FROM public.macro_indicators "
        "WHERE sma_20 IS NULL AND date > '2025-11-20' GROUP BY symbol"), c)
    print("  recent NULL sma_20 (warm-up excluded):")
    print(nulls.to_string(index=False) if len(nulls) else "     none")
