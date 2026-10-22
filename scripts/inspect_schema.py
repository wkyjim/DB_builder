"""Inspect existing database schema for public.macro table."""
import sys
sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
from db_builder.config import local_engine
from sqlalchemy import text

engine = local_engine()

with engine.connect() as conn:
    # Check public.macro table structure
    print("=" * 80)
    print("PUBLIC.MACRO TABLE STRUCTURE")
    print("=" * 80)
    
    query = text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'macro'
        ORDER BY ordinal_position
    """)
    rows = conn.execute(query).fetchall()
    for r in rows:
        print(f"  {r[0]}: {r[1]} (nullable={r[2]}, default={r[3]})")
    
    # Check constraints
    print("\n" + "=" * 80)
    print("PUBLIC.MACRO CONSTRAINTS")
    print("=" * 80)
    
    query = text("""
        SELECT constraint_name, constraint_type
        FROM information_schema.table_constraints
        WHERE table_schema = 'public' AND table_name = 'macro'
    """)
    rows = conn.execute(query).fetchall()
    for r in rows:
        print(f"  {r[0]}: {r[1]}")
    
    # Check existing USnYT=X data (using date column, not session_date)
    print("\n" + "=" * 80)
    print("EXISTING USnYT=X DATA (latest per symbol)")
    print("=" * 80)
    
    query = text("""
        SELECT symbol, close, date
        FROM public.macro
        WHERE symbol IN ('US2YT=X','US3YT=X','US5YT=X','US7YT=X','US10YT=X','US20YT=X','US30YT=X')
        AND date = (
            SELECT MAX(date) FROM public.macro m2 WHERE m2.symbol = public.macro.symbol
        )
        ORDER BY symbol
    """)
    rows = conn.execute(query).fetchall()
    for r in rows:
        print(f"  {r[0]}: close={r[1]}, date={r[2]}")
    
    # Check if any US*_MASSIVE symbols already exist
    print("\n" + "=" * 80)
    print("CHECK FOR EXISTING MASSIVE UST SYMBOLS")
    print("=" * 80)
    
    query = text("""
        SELECT DISTINCT symbol FROM public.macro WHERE symbol LIKE '%MASSIVE%'
    """)
    rows = conn.execute(query).fetchall()
    if rows:
        for r in rows:
            print(f"  Found: {r[0]}")
    else:
        print("  No existing MASSIVE symbols found")
    
    # Check total symbol count
    print("\n" + "=" * 80)
    print("TOTAL SYMBOL COUNT")
    print("=" * 80)
    
    query = text("SELECT COUNT(DISTINCT symbol) FROM public.macro")
    row = conn.execute(query).fetchone()
    print(f"  Total distinct symbols: {row[0]}")

print("\n" + "=" * 80)
print("SCHEMA INSPECTION COMPLETE")
print("=" * 80)
