"""Calculate all implemented technical indicators for 12 Investing.com symbols.

This script:
1. Inspects code for SMA, EMA, RSI implementations
2. Calculates all implemented indicators for all 12 symbols
3. Stores results in temporary database columns or logs
4. Validates results
5. Generates final report

AUTHORIZED: Calculate all implemented technical indicators
"""
import sys
import os
import json
import math
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
from db_builder.config import local_engine as _local_engine_func
from sqlalchemy import text

# Create engine instance
local_engine = _local_engine_func()

print("=" * 80)
print("CALCULATE ALL IMPLEMENTED TECHNICAL INDICATORS")
print("Target: 12 Investing.com-owned symbols")
print("=" * 80)

# All 12 symbols
SYMBOLS = [
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X",
    "BZ=F", "CL=F", "HK50", "KOR200c1", "CIHc1",
]

# ============================================================================
# PHASE 1: Inspect code for indicator implementations
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 1: INSPECT CODE FOR INDICATOR IMPLEMENTATIONS")
print("=" * 80)

macro_fetch_path = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts\macro_data_fetch.py"

indicators_found = {
    "SMA": False,
    "EMA": False,
    "RSI": False,
    "MACD": False,
    "Bollinger_Bands": False,
}

sma_periods = []
ema_periods = []
rsi_period = None

if os.path.exists(macro_fetch_path):
    with open(macro_fetch_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check for SMA
    if "sma" in content.lower() or "SMA" in content:
        indicators_found["SMA"] = True
        # Find SMA periods
        import re
        sma_matches = re.findall(r'sma[_\s]*(\d+)', content, re.IGNORECASE)
        sma_periods = list(set(int(m) for m in sma_matches))
        print(f"  SMA: ✅ Found (periods: {sma_periods})")
    
    # Check for EMA
    if "ema" in content.lower() or "EMA" in content:
        indicators_found["EMA"] = True
        # Find EMA periods
        import re
        ema_matches = re.findall(r'ema[_\s]*(\d+)', content, re.IGNORECASE)
        ema_periods = list(set(int(m) for m in ema_matches))
        print(f"  EMA: ✅ Found (periods: {ema_periods})")
    
    # Check for RSI
    if "rsi" in content.lower() or "RSI" in content:
        indicators_found["RSI"] = True
        # Find RSI period
        import re
        rsi_matches = re.findall(r'rsi[_\s]*(\d+)', content, re.IGNORECASE)
        if rsi_matches:
            rsi_period = int(rsi_matches[0])
        print(f"  RSI: ✅ Found (period: {rsi_period})")
    
    # Check for MACD
    if "macd" in content.lower() or "MACD" in content:
        indicators_found["MACD"] = True
        print(f"  MACD: ✅ Found")
    
    # Check for Bollinger Bands
    if "bollinger" in content.lower() or "Bollinger" in content:
        indicators_found["Bollinger_Bands"] = True
        print(f"  Bollinger Bands: ✅ Found")

print(f"\n  Summary:")
for indicator, found in indicators_found.items():
    status = "✅ IMPLEMENTED" if found else "❌ NOT IMPLEMENTED"
    print(f"    {indicator}: {status}")

# ============================================================================
# PHASE 2: Calculate all implemented indicators
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 2: CALCULATE ALL IMPLEMENTED INDICATORS")
print("=" * 80)

# Default periods if not found in code
if not sma_periods:
    sma_periods = [5, 10, 20, 50, 200]
if not ema_periods:
    ema_periods = [5, 10, 20, 50, 200]
if not rsi_period:
    rsi_period = 14

print(f"\n  Using periods:")
print(f"    SMA: {sma_periods}")
print(f"    EMA: {ema_periods}")
print(f"    RSI: {rsi_period}")

def calculate_sma(data: List[float], period: int) -> List[Optional[float]]:
    """Calculate Simple Moving Average."""
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
        else:
            window = data[i - period + 1:i + 1]
            if all(v is not None for v in window):
                result.append(round(sum(window) / period, 4))
            else:
                result.append(None)
    return result

def calculate_ema(data: List[float], period: int) -> List[Optional[float]]:
    """Calculate Exponential Moving Average."""
    result = []
    multiplier = 2 / (period + 1)
    
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            # First EMA is SMA
            window = data[:period]
            if all(v is not None for v in window):
                ema = sum(window) / period
                result.append(round(ema, 4))
            else:
                result.append(None)
        else:
            if data[i] is not None and result[i-1] is not None:
                ema = (data[i] - result[i-1]) * multiplier + result[i-1]
                result.append(round(ema, 4))
            else:
                result.append(None)
    return result

def calculate_rsi(data: List[float], period: int = 14) -> List[Optional[float]]:
    """Calculate Relative Strength Index."""
    result = []
    
    for i in range(len(data)):
        if i < period:
            result.append(None)
        else:
            # Calculate price changes
            changes = []
            for j in range(i - period + 1, i + 1):
                if j > 0 and data[j] is not None and data[j-1] is not None:
                    changes.append(data[j] - data[j-1])
            
            if len(changes) < period:
                result.append(None)
                continue
            
            gains = [max(0, c) for c in changes]
            losses = [max(0, -c) for c in changes]
            
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            
            if avg_loss == 0:
                result.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
                result.append(round(rsi, 4))
    
    return result

def calculate_bollinger_bands(data: List[float], period: int = 20, std_dev: float = 2.0) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Calculate Bollinger Bands (middle, upper, lower)."""
    middle = calculate_sma(data, period)
    upper = []
    lower = []
    
    for i in range(len(data)):
        if i < period - 1:
            upper.append(None)
            lower.append(None)
        else:
            window = data[i - period + 1:i + 1]
            if all(v is not None for v in window):
                mean = sum(window) / period
                variance = sum((x - mean) ** 2 for x in window) / period
                std = math.sqrt(variance)
                upper.append(round(mean + std_dev * std, 4))
                lower.append(round(mean - std_dev * std, 4))
            else:
                upper.append(None)
                lower.append(None)
    
    return middle, upper, lower

# Calculate indicators for all symbols
all_results = {}

for symbol in SYMBOLS:
    print(f"\n  [{symbol}]")
    
    with local_engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT date, close
                FROM public.macro
                WHERE symbol = :symbol
                ORDER BY date
            """),
            {"symbol": symbol},
        )
        rows = result.fetchall()
    
    dates = [row[0].strftime("%Y-%m-%d") for row in rows]
    closes = [float(row[1]) if row[1] is not None else None for row in rows]
    
    symbol_results = {
        "dates": dates,
        "closes": closes,
        "SMA": {},
        "EMA": {},
        "RSI": None,
        "Bollinger_Bands": None,
    }
    
    # Calculate SMA
    for period in sma_periods:
        sma_values = calculate_sma(closes, period)
        symbol_results["SMA"][period] = sma_values
        valid_count = sum(1 for v in sma_values if v is not None)
        print(f"    SMA({period}): {valid_count} values")
    
    # Calculate EMA
    for period in ema_periods:
        ema_values = calculate_ema(closes, period)
        symbol_results["EMA"][period] = ema_values
        valid_count = sum(1 for v in ema_values if v is not None)
        print(f"    EMA({period}): {valid_count} values")
    
    # Calculate RSI
    rsi_values = calculate_rsi(closes, rsi_period)
    symbol_results["RSI"] = rsi_values
    valid_count = sum(1 for v in rsi_values if v is not None)
    print(f"    RSI({rsi_period}): {valid_count} values")
    
    # Calculate Bollinger Bands
    bb_middle, bb_upper, bb_lower = calculate_bollinger_bands(closes, 20, 2.0)
    symbol_results["Bollinger_Bands"] = {
        "middle": bb_middle,
        "upper": bb_upper,
        "lower": bb_lower,
    }
    valid_count = sum(1 for v in bb_upper if v is not None)
    print(f"    Bollinger Bands(20, 2.0): {valid_count} values")
    
    all_results[symbol] = symbol_results

# ============================================================================
# PHASE 3: Store results in temporary table
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 3: STORE RESULTS IN TEMPORARY TABLE")
print("=" * 80)

with local_engine.begin() as conn:
    try:
        # Create temporary table for indicators
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS public.macro_indicators_temp (
                symbol VARCHAR(50),
                date DATE,
                close DECIMAL(12,4),
                sma_5 DECIMAL(12,4),
                sma_10 DECIMAL(12,4),
                sma_20 DECIMAL(12,4),
                sma_50 DECIMAL(12,4),
                sma_200 DECIMAL(12,4),
                ema_5 DECIMAL(12,4),
                ema_10 DECIMAL(12,4),
                ema_20 DECIMAL(12,4),
                ema_50 DECIMAL(12,4),
                ema_200 DECIMAL(12,4),
                rsi_14 DECIMAL(12,4),
                bb_middle DECIMAL(12,4),
                bb_upper DECIMAL(12,4),
                bb_lower DECIMAL(12,4),
                PRIMARY KEY (symbol, date)
            )
        """))
        
        # Clear existing data
        conn.execute(text("DELETE FROM public.macro_indicators_temp"))
        
        # Insert calculated indicators
        for symbol, results in all_results.items():
            dates = results["dates"]
            closes = results["closes"]
            
            for i, date in enumerate(dates):
                conn.execute(
                    text("""
                        INSERT INTO public.macro_indicators_temp
                        (symbol, date, close, sma_5, sma_10, sma_20, sma_50, sma_200,
                         ema_5, ema_10, ema_20, ema_50, ema_200, rsi_14,
                         bb_middle, bb_upper, bb_lower)
                        VALUES
                        (:symbol, :date, :close, :sma_5, :sma_10, :sma_20, :sma_50, :sma_200,
                         :ema_5, :ema_10, :ema_20, :ema_50, :ema_200, :rsi_14,
                         :bb_middle, :bb_upper, :bb_lower)
                    """),
                    {
                        "symbol": symbol,
                        "date": date,
                        "close": closes[i],
                        "sma_5": results["SMA"].get(5, [None]*len(dates))[i],
                        "sma_10": results["SMA"].get(10, [None]*len(dates))[i],
                        "sma_20": results["SMA"].get(20, [None]*len(dates))[i],
                        "sma_50": results["SMA"].get(50, [None]*len(dates))[i],
                        "sma_200": results["SMA"].get(200, [None]*len(dates))[i],
                        "ema_5": results["EMA"].get(5, [None]*len(dates))[i],
                        "ema_10": results["EMA"].get(10, [None]*len(dates))[i],
                        "ema_20": results["EMA"].get(20, [None]*len(dates))[i],
                        "ema_50": results["EMA"].get(50, [None]*len(dates))[i],
                        "ema_200": results["EMA"].get(200, [None]*len(dates))[i],
                        "rsi_14": results["RSI"][i] if results["RSI"] else None,
                        "bb_middle": results["Bollinger_Bands"]["middle"][i] if results["Bollinger_Bands"]["middle"] else None,
                        "bb_upper": results["Bollinger_Bands"]["upper"][i] if results["Bollinger_Bands"]["upper"] else None,
                        "bb_lower": results["Bollinger_Bands"]["lower"][i] if results["Bollinger_Bands"]["lower"] else None,
                    },
                )
        
        print(f"  ✅ Created temporary table public.macro_indicators_temp")
        print(f"  ✅ Inserted indicator data for all 12 symbols")
        
    except Exception as e:
        print(f"  ❌ Failed to create temporary table: {e}")
        raise

# ============================================================================
# PHASE 4: Validation
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 4: VALIDATION")
print("=" * 80)

with local_engine.connect() as conn:
    # Count rows in temporary table
    result = conn.execute(text("SELECT COUNT(*) FROM public.macro_indicators_temp"))
    count = result.fetchone()[0]
    print(f"\n  Total rows in temporary table: {count}")
    
    # Sample data
    result = conn.execute(text("""
        SELECT symbol, date, close, sma_20, ema_20, rsi_14, bb_upper, bb_lower
        FROM public.macro_indicators_temp
        WHERE symbol = 'US10YT=X'
        ORDER BY date DESC
        LIMIT 5
    """))
    rows = result.fetchall()
    
    print(f"\n  Sample data (US10YT=X, last 5 rows):")
    for row in rows:
        print(f"    {row[1]}: close={row[2]}, sma_20={row[3]}, ema_20={row[4]}, rsi_14={row[5]}, bb_upper={row[6]}, bb_lower={row[7]}")
    
    # Check for NULL values
    result = conn.execute(text("""
        SELECT COUNT(*) FROM public.macro_indicators_temp
        WHERE sma_20 IS NULL
    """))
    null_sma = result.fetchone()[0]
    
    result = conn.execute(text("""
        SELECT COUNT(*) FROM public.macro_indicators_temp
        WHERE rsi_14 IS NULL
    """))
    null_rsi = result.fetchone()[0]
    
    print(f"\n  NULL value counts:")
    print(f"    SMA(20): {null_sma} NULL values")
    print(f"    RSI(14): {null_rsi} NULL values")

# ============================================================================
# FINAL REPORT
# ============================================================================
print("\n" + "=" * 80)
print("FINAL REPORT")
print("=" * 80)

print(f"\n  INDICATORS_CALCULATED = SMA, EMA, RSI, Bollinger Bands")
print(f"  SMA_PERIODS = {sma_periods}")
print(f"  EMA_PERIODS = {ema_periods}")
print(f"  RSI_PERIOD = {rsi_period}")
print(f"  BOLLINGER_BANDS = (20, 2.0)")
print(f"  SYMBOLS_PROCESSED = 12")
print(f"  TEMPORARY_TABLE = public.macro_indicators_temp")
print(f"  TOTAL_ROWS = {count}")
print(f"  NEON_ACCESSED = NO")
print(f"  PRODUCTION_ROUTING_CHANGED = NO")

print(f"\n{'='*80}")
print("ALL TECHNICAL INDICATORS CALCULATED")
print(f"{'='*80}")
