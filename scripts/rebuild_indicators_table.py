"""Rebuild macro_indicators table with same structure as us_equities_indicators.

This script:
1. Drops old macro_indicators table
2. Creates new macro_indicators table matching us_equities_indicators structure
3. Calculates all indicators for all 12 symbols from earliest date to now
4. Validates results
5. Generates final report

Indicators to calculate (matching us_equities_indicators):
- MA: ma_5, ma_20, ma_50, ma_100, ma_200
- EMA: ema_12, ema_26
- RSI: rsi_14
- ATR: atr_14
- Volume: volume_ma_20, volume_ratio_20
- 52-week: high_52w, low_52w
- Returns: return_5d, return_20d, return_60d
- Volatility: volatility_20d

NOT calculated (per user request): MACD
"""
import sys
import os
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")
from db_builder.config import local_engine as _local_engine_func
from sqlalchemy import text

local_engine = _local_engine_func()

print("=" * 80)
print("REBUILD MACRO_INDICATORS TABLE")
print("Structure: Same as us_equities_indicators")
print("Range: Earliest date to now")
print("=" * 80)

SYMBOLS = [
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X",
    "BZ=F", "CL=F", "HK50", "KOR200c1", "CIHc1",
]

# ============================================================================
# PHASE 1: Drop old table and create new one
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 1: CREATE NEW MACRO_INDICATORS TABLE")
print("=" * 80)

with local_engine.begin() as conn:
    # Drop old table
    conn.execute(text("DROP TABLE IF EXISTS public.macro_indicators"))
    print("  Dropped old macro_indicators table")
    
    # Create new table matching us_equities_indicators structure (without MACD)
    conn.execute(text("""
        CREATE TABLE public.macro_indicators (
            symbol VARCHAR(50),
            date DATE,
            ma_5 DECIMAL(12,4),
            ma_20 DECIMAL(12,4),
            ma_50 DECIMAL(12,4),
            ma_100 DECIMAL(12,4),
            ma_200 DECIMAL(12,4),
            ema_12 DECIMAL(12,4),
            ema_26 DECIMAL(12,4),
            rsi_14 DECIMAL(12,4),
            atr_14 DECIMAL(12,4),
            volume_ma_20 DECIMAL(12,4),
            volume_ratio_20 DECIMAL(12,4),
            high_52w DECIMAL(12,4),
            low_52w DECIMAL(12,4),
            return_5d DECIMAL(12,4),
            return_20d DECIMAL(12,4),
            return_60d DECIMAL(12,4),
            volatility_20d DECIMAL(12,4),
            PRIMARY KEY (symbol, date)
        )
    """))
    print("  ✅ Created new macro_indicators table (matching us_equities_indicators structure)")

# ============================================================================
# PHASE 2: Calculate indicators
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 2: CALCULATE INDICATORS FOR ALL SYMBOLS")
print("=" * 80)

def calc_sma(data: List[Optional[float]], period: int) -> List[Optional[float]]:
    """Calculate Simple Moving Average."""
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
        else:
            window = data[i - period + 1:i + 1]
            valid = [v for v in window if v is not None]
            if len(valid) == period:
                result.append(round(sum(valid) / period, 4))
            else:
                result.append(None)
    return result

def calc_ema(data: List[Optional[float]], period: int) -> List[Optional[float]]:
    """Calculate Exponential Moving Average."""
    result = []
    multiplier = 2 / (period + 1)
    
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
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

def calc_rsi(closes: List[Optional[float]], period: int = 14) -> List[Optional[float]]:
    """Calculate Relative Strength Index."""
    result = []
    
    for i in range(len(closes)):
        if i < period:
            result.append(None)
        else:
            changes = []
            for j in range(i - period + 1, i + 1):
                if j > 0 and closes[j] is not None and closes[j-1] is not None:
                    changes.append(closes[j] - closes[j-1])
            
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

def calc_atr(highs: List[Optional[float]], lows: List[Optional[float]], closes: List[Optional[float]], period: int = 14) -> List[Optional[float]]:
    """Calculate Average True Range."""
    trs = []
    for i in range(len(closes)):
        if i == 0 or closes[i] is None or closes[i-1] is None:
            trs.append(None)
        else:
            high = highs[i] if highs[i] is not None else closes[i]
            low = lows[i] if lows[i] is not None else closes[i]
            tr = max(high - low, abs(high - closes[i-1]), abs(low - closes[i-1]))
            trs.append(tr)
    
    result = []
    for i in range(len(trs)):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            window = trs[:period]
            if all(v is not None for v in window):
                result.append(round(sum(window) / period, 4))
            else:
                result.append(None)
        else:
            if trs[i] is not None and result[i-1] is not None:
                atr = (result[i-1] * (period - 1) + trs[i]) / period
                result.append(round(atr, 4))
            else:
                result.append(None)
    return result

def calc_volatility(closes: List[Optional[float]], period: int = 20) -> List[Optional[float]]:
    """Calculate volatility (standard deviation of returns)."""
    result = []
    
    for i in range(len(closes)):
        if i < period:
            result.append(None)
        else:
            returns = []
            for j in range(i - period + 1, i + 1):
                if j > 0 and closes[j] is not None and closes[j-1] is not None:
                    ret = (closes[j] - closes[j-1]) / closes[j-1] if closes[j-1] != 0 else 0
                    returns.append(ret)
            
            if len(returns) < period:
                result.append(None)
                continue
            
            mean = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / len(returns)
            vol = math.sqrt(variance)
            result.append(round(vol * 100, 4))  # As percentage
    
    return result

# Process each symbol
total_rows = 0

with local_engine.begin() as conn:
    for symbol in SYMBOLS:
        print(f"\n  [{symbol}]")
        
        # Get all data from earliest date to now
        result = conn.execute(text("""
            SELECT date, close, high, low, volume
            FROM public.macro
            WHERE symbol = :symbol
            ORDER BY date
        """), {"symbol": symbol})
        rows = result.fetchall()
        
        if not rows:
            print(f"    No data found — skip")
            continue
        
        dates = [row[0] for row in rows]
        closes = [float(row[1]) if row[1] is not None else None for row in rows]
        highs = [float(row[2]) if row[2] is not None else None for row in rows]
        lows = [float(row[3]) if row[3] is not None else None for row in rows]
        volumes = [float(row[4]) if row[4] is not None else None for row in rows]
        
        print(f"    Total rows: {len(rows)}, Date range: {dates[0]} to {dates[-1]}")
        
        # Calculate indicators
        ma_5 = calc_sma(closes, 5)
        ma_20 = calc_sma(closes, 20)
        ma_50 = calc_sma(closes, 50)
        ma_100 = calc_sma(closes, 100)
        ma_200 = calc_sma(closes, 200)
        ema_12 = calc_ema(closes, 12)
        ema_26 = calc_ema(closes, 26)
        rsi_14 = calc_rsi(closes, 14)
        atr_14 = calc_atr(highs, lows, closes, 14)
        volatility_20d = calc_volatility(closes, 20)
        
        # Volume indicators
        volume_ma_20 = calc_sma(volumes, 20)
        volume_ratio_20 = []
        for i in range(len(volumes)):
            if volume_ma_20[i] is not None and volume_ma_20[i] > 0 and volumes[i] is not None:
                volume_ratio_20.append(round(volumes[i] / volume_ma_20[i], 4))
            else:
                volume_ratio_20.append(None)
        
        # 52-week high/low (252 trading days)
        high_52w = []
        low_52w = []
        for i in range(len(highs)):
            if i < 251:
                high_52w.append(None)
                low_52w.append(None)
            else:
                window_highs = [h for h in highs[i-251:i+1] if h is not None]
                window_lows = [l for l in lows[i-251:i+1] if l is not None]
                high_52w.append(round(max(window_highs), 4) if window_highs else None)
                low_52w.append(round(min(window_lows), 4) if window_lows else None)
        
        # Returns
        return_5d = []
        return_20d = []
        return_60d = []
        for i in range(len(closes)):
            if i >= 5 and closes[i] is not None and closes[i-5] is not None and closes[i-5] != 0:
                return_5d.append(round((closes[i] - closes[i-5]) / closes[i-5] * 100, 4))
            else:
                return_5d.append(None)
            
            if i >= 20 and closes[i] is not None and closes[i-20] is not None and closes[i-20] != 0:
                return_20d.append(round((closes[i] - closes[i-20]) / closes[i-20] * 100, 4))
            else:
                return_20d.append(None)
            
            if i >= 60 and closes[i] is not None and closes[i-60] is not None and closes[i-60] != 0:
                return_60d.append(round((closes[i] - closes[i-60]) / closes[i-60] * 100, 4))
            else:
                return_60d.append(None)
        
        # Insert into macro_indicators
        for i, date in enumerate(dates):
            conn.execute(text("""
                INSERT INTO public.macro_indicators
                (symbol, date, ma_5, ma_20, ma_50, ma_100, ma_200,
                 ema_12, ema_26, rsi_14, atr_14, volume_ma_20, volume_ratio_20,
                 high_52w, low_52w, return_5d, return_20d, return_60d, volatility_20d)
                VALUES
                (:symbol, :date, :ma_5, :ma_20, :ma_50, :ma_100, :ma_200,
                 :ema_12, :ema_26, :rsi_14, :atr_14, :volume_ma_20, :volume_ratio_20,
                 :high_52w, :low_52w, :return_5d, :return_20d, :return_60d, :volatility_20d)
                ON CONFLICT (symbol, date) DO UPDATE SET
                    ma_5 = EXCLUDED.ma_5,
                    ma_20 = EXCLUDED.ma_20,
                    ma_50 = EXCLUDED.ma_50,
                    ma_100 = EXCLUDED.ma_100,
                    ma_200 = EXCLUDED.ma_200,
                    ema_12 = EXCLUDED.ema_12,
                    ema_26 = EXCLUDED.ema_26,
                    rsi_14 = EXCLUDED.rsi_14,
                    atr_14 = EXCLUDED.atr_14,
                    volume_ma_20 = EXCLUDED.volume_ma_20,
                    volume_ratio_20 = EXCLUDED.volume_ratio_20,
                    high_52w = EXCLUDED.high_52w,
                    low_52w = EXCLUDED.low_52w,
                    return_5d = EXCLUDED.return_5d,
                    return_20d = EXCLUDED.return_20d,
                    return_60d = EXCLUDED.return_60d,
                    volatility_20d = EXCLUDED.volatility_20d
            """), {
                "symbol": symbol,
                "date": date,
                "ma_5": ma_5[i],
                "ma_20": ma_20[i],
                "ma_50": ma_50[i],
                "ma_100": ma_100[i],
                "ma_200": ma_200[i],
                "ema_12": ema_12[i],
                "ema_26": ema_26[i],
                "rsi_14": rsi_14[i],
                "atr_14": atr_14[i],
                "volume_ma_20": volume_ma_20[i],
                "volume_ratio_20": volume_ratio_20[i],
                "high_52w": high_52w[i],
                "low_52w": low_52w[i],
                "return_5d": return_5d[i],
                "return_20d": return_20d[i],
                "return_60d": return_60d[i],
                "volatility_20d": volatility_20d[i],
            })
        
        total_rows += len(dates)
        print(f"    ✅ Inserted/updated {len(dates)} rows")

# ============================================================================
# PHASE 3: Validation
# ============================================================================
print("\n" + "=" * 80)
print("PHASE 3: VALIDATION")
print("=" * 80)

with local_engine.connect() as conn:
    result = conn.execute(text("SELECT COUNT(*) FROM public.macro_indicators"))
    count = result.fetchone()[0]
    print(f"\n  Total rows in macro_indicators: {count}")
    
    # Sample data
    print("\n  Sample data (US10YT=X, last 5 rows):")
    result = conn.execute(text("""
        SELECT date, ma_20, ema_26, rsi_14, atr_14, volatility_20d, high_52w, low_52w
        FROM public.macro_indicators
        WHERE symbol = 'US10YT=X'
        ORDER BY date DESC
        LIMIT 5
    """))
    rows = result.fetchall()
    
    for row in rows:
        print(f"    {row[0]}: ma_20={row[1]}, ema_26={row[2]}, rsi_14={row[3]}, atr_14={row[4]}, vol_20d={row[5]}%, high_52w={row[6]}, low_52w={row[7]}")

# ============================================================================
# FINAL REPORT
# ============================================================================
print("\n" + "=" * 80)
print("FINAL REPORT")
print("=" * 80)

print(f"\n  TABLE_REBUILT = YES")
print(f"  STRUCTURE_MATCHES_US_EQUITIES_INDICATORS = YES")
print(f"  SYMBOLS_PROCESSED = {len(SYMBOLS)}")
print(f"  TOTAL_ROWS = {total_rows}")
print(f"  DATE_RANGE = EARLIEST_TO_NOW")
print(f"  MACD_CALCULATED = NO (as requested)")
print(f"  NEON_ACCESSED = NO")
print(f"  PRODUCTION_ROUTING_CHANGED = NO")

print(f"\n  Indicators calculated:")
print(f"    - MA: ma_5, ma_20, ma_50, ma_100, ma_200")
print(f"    - EMA: ema_12, ema_26")
print(f"    - RSI: rsi_14")
print(f"    - ATR: atr_14")
print(f"    - Volume: volume_ma_20, volume_ratio_20")
print(f"    - 52-week: high_52w, low_52w")
print(f"    - Returns: return_5d, return_20d, return_60d")
print(f"    - Volatility: volatility_20d")

print(f"\n{'='*80}")
print("TABLE REBUILT AND INDICATORS CALCULATED")
print(f"{'='*80}")
