"""(1) verify stored values against the live API
   (2) recalc indicators for the two new symbols
"""
import sys

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts")

from db_builder import config
from db_builder.config import local_engine
from macro_data_fetch import OILPRICEAPI_ASSETS, OILPRICEAPI_MAPPING, fetch_oilpriceapi_history
from sqlalchemy import text
import pandas as pd

eng = local_engine()

print("=" * 92)
print("1. STORED vs LIVE API")
print("=" * 92)
with eng.connect() as c:
    for symbol in sorted(OILPRICEAPI_ASSETS):
        df = pd.read_sql(text(
            "SELECT date, close, prev_close FROM public.macro "
            "WHERE symbol=:s ORDER BY date"), c, params={"s": symbol})
        obs = dict(fetch_oilpriceapi_history(OILPRICEAPI_MAPPING[symbol]))
        stored = {d.strftime("%Y-%m-%d"): float(v) for d, v in zip(df["date"], df["close"])}
        common = sorted(set(stored) & set(obs))
        mism = [(d, stored[d], obs[d]) for d in common
                if abs(stored[d] - float(obs[d])) > 1e-6]
        print()
        print("  {}".format(symbol))
        print("     stored rows      : {}".format(len(df)))
        print("     api observations  : {}".format(len(obs)))
        print("     common dates      : {}".format(len(common)))
        print("     value mismatches  : {}".format(len(mism)))
        if mism:
            for d, a, b in mism[:10]:
                print("        {} stored={} api={}".format(d, a, b))
        only_stored = sorted(set(stored) - set(obs))
        only_api = sorted(set(obs) - set(stored))
        if only_stored:
            print("     stored-but-not-api: {}  (first 5: {})".format(
                len(only_stored), only_stored[:5]))
        if only_api:
            print("     api-but-not-stored: {}  (first 5: {})".format(
                len(only_api), only_api[:5]))
        # prev_close continuity
        bad = 0
        rows = list(zip(df["date"], df["close"], df["prev_close"]))
        for i in range(1, len(rows)):
            d, cl, pc = rows[i]
            pd_, pcl, _ = rows[i - 1]
            if pc is None or abs(float(pc) - float(pcl)) > 1e-6:
                bad += 1
        print("     prev_close chain breaks (row N vs N-1 close): {}".format(bad))

print()
print("=" * 92)
print("2. RECALCULATE INDICATORS")
print("=" * 92)
from db_builder import macro_indicators as mi
res = mi.recalculate_indicators_for_symbols(eng, sorted(OILPRICEAPI_ASSETS))
print("  result: {}".format(res))
