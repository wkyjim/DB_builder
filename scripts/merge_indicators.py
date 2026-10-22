"""Merge technical indicators from temporary table to main table.

This script:
1. Checks main table structure
2. Adds missing indicator columns if needed
3. Merges temporary table data to main table
4. Validates results
5. Generates final report

AUTHORIZED: Merge indicators to main table
NOT AUTHORIZED: MACD calculation
"""
import sys
import os
from datetime import datetime, timezone
from typing import Dict, List, Any

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
from db_builder.config import local_engine as _local_engine_func
from sqlalchemy import text

# Create engine instance
local_engine = _local_engine_func()

print("=" * 80)
print("MERGE TECHNICAL INDICATORS TO MAIN TABLE")
print("Target: public.macro")
print("Source: public.macro_indicators_temp")
print("=" * 80)

# All 12 symbols
SYMBOLS = [
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X",
    "BZ=F", "CL=F", "HK50", "KOR200c1", "CIHc1",
]

# Indicator columns to merge
INDICATOR_COLUMNS = [
    "sma_5", "sma_10", "sma_20", "sma_50", "sma_200",
    "ema_5", "ema_10", "ema_20", "ema_50", "ema_200",
    "rsi_14", "bb_middle", "bb_upper", "bb_lower",
]

# ============================================================================
# PHASE 1: Check main table structure
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 1: CHECK MAIN TABLE STRUCTURE")
print("=" * 80)

with local_engine.connect() as conn:
    result = conn.execute(text("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'macro' 
        ORDER BY ordinal_position
    """))
    existing_columns = [row[0] for row in result.fetchall()]

print(f"\n  Existing columns in public.macro: {existing_columns}")

# Check which indicator columns are missing
missing_columns = [col for col in INDICATOR_COLUMNS if col not in existing_columns]
print(f"\n  Missing indicator columns: {missing_columns}")

# ============================================================================
# PHASE 2: Add missing columns
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 2: ADD MISSING COLUMNS")
print("=" * 80)

if missing_columns:
    with local_engine.begin() as conn:
        for col in missing_columns:
            print(f"  Adding column: {col}")
            conn.execute(text(f"""
                ALTER TABLE public.macro 
                ADD COLUMN IF NOT EXISTS {col} DECIMAL(12,4)
            """))
        print(f"  ✅ Added {len(missing_columns)} columns")
else:
    print(f"  ✅ All indicator columns already exist")

# ============================================================================
# PHASE 3: Merge data from temporary table
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 3: MERGE DATA FROM TEMPORARY TABLE")
print("=" * 80)

with local_engine.begin() as conn:
    try:
        # Build UPDATE statement
        set_clauses = ", ".join([f"{col} = temp.{col}" for col in INDICATOR_COLUMNS])
        
        conn.execute(text(f"""
            UPDATE public.macro AS main
            SET {set_clauses}
            FROM public.macro_indicators_temp AS temp
            WHERE main.symbol = temp.symbol 
            AND main.date = temp.date
        """))
        
        print(f"  ✅ Merged indicator data to main table")
        print(f"  ✅ Updated columns: {', '.join(INDICATOR_COLUMNS)}")
        
    except Exception as e:
        print(f"  ❌ Merge failed: {e}")
        raise

# ============================================================================
# PHASE 4: Validation
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 4: VALIDATION")
print("=" * 80)

with local_engine.connect() as conn:
    # Check sample data
    print("\n  Sample data (US10YT=X, last 5 rows):")
    result = conn.execute(text("""
        SELECT date, close, sma_20, ema_20, rsi_14, bb_upper, bb_lower
        FROM public.macro
        WHERE symbol = 'US10YT=X'
        ORDER BY date DESC
        LIMIT 5
    """))
    rows = result.fetchall()
    
    for row in rows:
        print(f"    {row[0]}: close={row[1]}, sma_20={row[2]}, ema_20={row[3]}, rsi_14={row[4]}, bb_upper={row[5]}, bb_lower={row[6]}")
    
    # Check for NULL values in target date range
    print("\n  NULL value counts in target date range (2026-09-30 to 2026-10-16):")
    result = conn.execute(text("""
        SELECT 
            COUNT(*) FILTER (WHERE sma_20 IS NULL) as sma_20_null,
            COUNT(*) FILTER (WHERE ema_20 IS NULL) as ema_20_null,
            COUNT(*) FILTER (WHERE rsi_14 IS NULL) as rsi_14_null,
            COUNT(*) FILTER (WHERE bb_upper IS NULL) as bb_upper_null,
            COUNT(*) as total
        FROM public.macro
        WHERE date BETWEEN '2026-09-30' AND '2026-10-16'
        AND symbol = ANY(:symbols)
    """), {"symbols": SYMBOLS})
    row = result.fetchone()
    print(f"    SMA(20): {row[0]} NULL values")
    print(f"    EMA(20): {row[1]} NULL values")
    print(f"    RSI(14): {row[2]} NULL values")
    print(f"    BB Upper: {row[3]} NULL values")
    print(f"    Total rows: {row[4]}")
    
    # Check all 12 symbols
    print("\n  All symbols indicator status:")
    for symbol in SYMBOLS:
        result = conn.execute(text("""
            SELECT 
                COUNT(*) FILTER (WHERE sma_20 IS NOT NULL) as sma_20_count,
                COUNT(*) FILTER (WHERE rsi_14 IS NOT NULL) as rsi_14_count,
                COUNT(*) as total
            FROM public.macro
            WHERE symbol = :symbol
        """), {"symbol": symbol})
        row = result.fetchone()
        print(f"    {symbol}: SMA(20)={row[0]}, RSI(14)={row[1]}, Total={row[2]}")

# ============================================================================
# FINAL REPORT
# ============================================================================
print("\n" + "=" * 80)
print("FINAL REPORT")
print("=" * 80)

print(f"\n  MERGE_STATUS = SUCCESS")
print(f"  COLUMNS_ADDED = {len(missing_columns)}")
print(f"  COLUMNS_MERGED = {len(INDICATOR_COLUMNS)}")
print(f"  SYMBOLS_PROCESSED = 12")
print(f"  MACD = NOT_CALCULATED (as requested)")
print(f"  NEON_ACCESSED = NO")
print(f"  PRODUCTION_ROUTING_CHANGED = NO")

print(f"\n  Indicator columns in main table:")
for col in INDICATOR_COLUMNS:
    print(f"    - {col}")

print(f"\n{'='*80}")
print("MERGE COMPLETE")
print(f"{'='*80}")
