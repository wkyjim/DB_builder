"""Dump public.macro schema + existing CMT/NIY/FNDX rows so inserts mirror them exactly."""
import sys

from sqlalchemy import text

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
from db_builder.config import local_engine as _f

eng = _f()

print("=" * 88)
print("public.macro schema")
print("=" * 88)

with eng.connect() as conn:
    for row in conn.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name='macro' AND table_schema='public'
        ORDER BY ordinal_position
    """)).fetchall():
        print("  {:<22} {:<28} null={:<4} default={}".format(*row))

    print()
    print("  --- constraints / indexes ---")
    for row in conn.execute(text("""
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint WHERE conrelid='public.macro'::regclass
    """)).fetchall():
        print("  {:<32} {}".format(*row))

    print()
    print("  --- existing CMT rows (full) ---")
    cols = [r[0] for r in conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='macro' AND table_schema='public' ORDER BY ordinal_position
    """)).fetchall()]
    print("  columns:", cols)
    print()
    for row in conn.execute(text("""
        SELECT * FROM public.macro WHERE symbol='US10YT_CMT' ORDER BY date DESC LIMIT 3
    """)).fetchall():
        print("  ", dict(zip(cols, row)))

    print()
    print("  --- existing NIY=F rows (full) ---")
    for row in conn.execute(text("""
        SELECT * FROM public.macro WHERE symbol='NIY=F' ORDER BY date DESC LIMIT 3
    """)).fetchall():
        print("  ", dict(zip(cols, row)))

    print()
    print("  --- existing ^NDX rows (full) ---")
    for row in conn.execute(text("""
        SELECT * FROM public.macro WHERE symbol='^NDX' ORDER BY date DESC LIMIT 3
    """)).fetchall():
        print("  ", dict(zip(cols, row)))

    print()
    print("  --- existing ^KS11 rows (full) ---")
    for row in conn.execute(text("""
        SELECT * FROM public.macro WHERE symbol='^KS11' ORDER BY date DESC LIMIT 3
    """)).fetchall():
        print("  ", dict(zip(cols, row)))

    print()
    print("  --- is there a source_definition column populated? distinct values ---")
    for row in conn.execute(text("""
        SELECT COALESCE(source_definition,'<NULL>') AS sd, COUNT(*)
        FROM public.macro GROUP BY sd ORDER BY COUNT(*) DESC
    """)).fetchall():
        print("  {:<48} {}".format(*row))

    print()
    print("  --- all columns, so I know what to set ---")
    for row in conn.execute(text("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name='macro' AND table_schema='public' ORDER BY ordinal_position
    """)).fetchall():
        print("    {:<24} {}".format(*row))
