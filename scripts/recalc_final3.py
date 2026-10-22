"""Recalculate indicators for every macro symbol using the real entry point."""
import sys

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")

from sqlalchemy import text
from db_builder import config

engine = config.local_engine()

with engine.connect() as conn:
    symbols = [r[0] for r in conn.execute(text(
        "SELECT DISTINCT symbol FROM public.macro ORDER BY symbol")).fetchall()]

print("=" * 92)
print("recalculating indicators for {} symbols".format(len(symbols)))
print("=" * 92)

from db_builder.macro_indicators import recalculate_macro_indicators

result = recalculate_macro_indicators(engine, symbols=symbols)

print()
print("=" * 92)
print("result")
print("=" * 92)
if isinstance(result, dict):
    for k, v in result.items():
        print("  {:<28} {}".format(k, v))
else:
    print("  " + repr(result))

with engine.connect() as conn:
    total = conn.execute(text(
        "SELECT count(DISTINCT symbol) FROM public.macro_indicators")).scalar()
    mx = conn.execute(text(
        "SELECT max(volume_ma_20) FROM public.macro_indicators "
        " WHERE volume_ma_20::text <> 'NaN'")).scalar()
    nan = conn.execute(text(
        "SELECT count(*) FROM public.macro_indicators WHERE volume_ma_20::text = 'NaN'")).scalar()
    print()
    print("  symbols in macro_indicators : {}".format(total))
    print("  max(volume_ma_20)           : {:,.2f}".format(mx or 0))
    print("  NaN volume_ma_20 rows       : {}".format(nan))
