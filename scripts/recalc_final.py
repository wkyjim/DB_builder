"""Recalculate indicators for all symbols now that volume_ma_20 can hold large values."""
import sys
import traceback

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")

from sqlalchemy import text
from db_builder import config

engine = config.local_engine()

with engine.connect() as conn:
    nan = conn.execute(text(
        "SELECT count(*) FROM public.macro_indicators WHERE volume_ma_20::text = 'NaN'")).scalar()
    print("NaN volume_ma_20 rows:", nan)

symbols = [r[0] for r in engine.connect().execute(text(
    "SELECT DISTINCT symbol FROM public.macro_prices ORDER BY symbol")).fetchall()]

print("symbols:", len(symbols))
print("=" * 92)

from db_builder.macro_indicators import recalculate_symbol

ok, failed = [], []
for s in symbols:
    try:
        recalculate_symbol(s, engine)
        ok.append(s)
    except Exception as exc:
        failed.append((s, "{}: {}".format(type(exc).__name__, exc)))
        traceback.print_exc()

print()
print("=" * 92)
print("recalculated : {}".format(len(ok)))
print("failed       : {}".format(len(failed)))
print("=" * 92)
for s, err in failed:
    print("  {:<14} {}".format(s, err[:110]))

with engine.connect() as conn:
    total = conn.execute(text(
        "SELECT count(DISTINCT symbol) FROM public.macro_indicators")).scalar()
    print()
    print("  symbols now in macro_indicators:", total)
