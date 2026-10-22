"""Recalculate indicators for every macro symbol now that volume_ma_20 is widened."""
import sys
import traceback

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

from db_builder.macro_indicators import recalculate_symbol

ok, failed = [], []
for s in symbols:
    try:
        recalculate_symbol(s, engine)
        ok.append(s)
    except Exception as exc:
        failed.append((s, "{}: {}".format(type(exc).__name__, exc)))

print()
print("=" * 92)
print("recalculated : {}".format(len(ok)))
print("failed       : {}".format(len(failed)))
print("=" * 92)
for s, err in failed:
    print("  {:<14} {}".format(s, err[:120]))
if not failed:
    print("  (none)")

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
