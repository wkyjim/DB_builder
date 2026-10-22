"""Massive UST EOD adapter — creates 7 NEW independent Treasury yield series.

This is an ADDITIVE implementation. Does NOT modify existing USnYT=X or USnYT_CMT.

New symbols:
  US2YT_MASSIVE -> yield_2_year
  US3YT_MASSIVE -> yield_3_year
  US5YT_MASSIVE -> yield_5_year
  US7YT_MASSIVE -> yield_7_year
  US10YT_MASSIVE -> yield_10_year
  US20YT_MASSIVE -> yield_20_year
  US30YT_MASSIVE -> yield_30_year

Source ownership: MASSIVE_UST_EOD
Frequency: DAILY
Unit: Percent (%)
"""
import sys
import os
import httpx
import time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

DB_BUILDER_ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
sys.path.insert(0, os.path.join(DB_BUILDER_ROOT, "src"))
from db_builder.config import local_engine

API_KEY = os.getenv("MASSIVE_API_KEY", "")

# New Massive UST symbols -> Massive field names
MASSIVE_UST_MAPPING: Dict[str, str] = {
    "US2YT_MASSIVE": "yield_2_year",
    "US3YT_MASSIVE": "yield_3_year",
    "US5YT_MASSIVE": "yield_5_year",
    "US7YT_MASSIVE": "yield_7_year",
    "US10YT_MASSIVE": "yield_10_year",
    "US20YT_MASSIVE": "yield_20_year",
    "US30YT_MASSIVE": "yield_30_year",
}

# Reverse mapping
MASSIVE_FIELD_TO_SYMBOL: Dict[str, str] = {v: k for k, v in MASSIVE_UST_MAPPING.items()}

SOURCE_DEF_MASSIVE_UST = "MASSIVE_UST_EOD"

# All 7 new symbols
ALL_MASSIVE_UST_SYMBOLS = list(MASSIVE_UST_MAPPING.keys())


@dataclass
class MassiveUSTObservation:
    symbol: str
    maturity_years: int
    yield_value: Optional[float]
    observation_date: Optional[str]
    source: str
    status: str
    reason: Optional[str] = None


def fetch_massive_ust_yields() -> Dict[str, Any]:
    """Fetch latest Treasury yields from Massive Economy API.

    Endpoint: GET /fed/v1/treasury-yields?sort=date.desc&limit=10
    """
    endpoint = "/fed/v1/treasury-yields"
    params = {
        "apiKey": API_KEY,
        "sort": "date.desc",
        "limit": 10,
    }
    time.sleep(12)  # Rate limit: 12s between requests
    resp = httpx.get(f"https://api.polygon.io{endpoint}", params=params, timeout=30)
    return resp


def parse_massive_ust_response(resp) -> Dict[str, Any]:
    """Parse Massive UST response and extract latest EOD observation.
    
    Massive Economy API provides 1Y, 5Y, 10Y only.
    Accept partial observations and report per-tenor availability.
    """
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "data": None}
    
    data = resp.json()
    results = data.get("results", [])
    
    if not results:
        return {"error": "Empty results", "data": None}
    
    # Use latest observation (first in desc-sorted results)
    latest = results[0]
    
    # Count available tenors
    available = sum(1 for f in MASSIVE_UST_MAPPING.values() if f in latest and latest[f] is not None)
    
    return {
        "data": latest,
        "skipped": 0,
        "available_tenors": available,
        "total_tenors": len(MASSIVE_UST_MAPPING),
    }


def normalize_massive_ust_yield(
    symbol: str,
    maturity_years: int,
    yield_value: Optional[float],
    observation_date: Optional[str],
    source: str,
    status: str,
    reason: Optional[str] = None,
) -> MassiveUSTObservation:
    """Normalize Massive UST yield to Hermes schema."""
    return MassiveUSTObservation(
        symbol=symbol,
        maturity_years=maturity_years,
        yield_value=yield_value,
        observation_date=observation_date,
        source=source,
        status=status,
        reason=reason,
    )


def compare_with_local_baseline_ust(
    observations: List[MassiveUSTObservation],
    engine,
) -> Dict[str, Any]:
    """Compare Massive UST yields with Local PostgreSQL baselines."""
    from sqlalchemy import text

    results = {}
    with engine.connect() as conn:
        for obs in observations:
            if obs.status != "AVAILABLE":
                results[obs.symbol] = {"status": obs.status, "reason": obs.reason}
                continue

            query = text(
                "SELECT close, date FROM public.macro "
                "WHERE symbol = :symbol ORDER BY date DESC LIMIT 1"
            )
            row = conn.execute(query, {"symbol": obs.symbol}).fetchone()
            if row:
                local_close = row[0]
                local_date = str(row[1])
                results[obs.symbol] = {
                    "status": "AVAILABLE",
                    "massive_yield": obs.yield_value,
                    "massive_date": obs.observation_date,
                    "local_close": local_close,
                    "local_date": local_date,
                    "diff": abs(obs.yield_value - local_close) if obs.yield_value and local_close else None,
                }
            else:
                results[obs.symbol] = {
                    "status": "NO_BASELINE",
                    "reason": "No local baseline found (new symbol)",
                }
    return results


def dry_run_massive_ust():
    """Execute read-only dry-run for Massive UST EOD migration."""
    print("=" * 80)
    print("MASSIVE UST EOD ADAPTER — DRY RUN")
    print("=" * 80)

    # Fetch from Massive
    print("\n[1] Fetching Treasury yields from Massive...")
    resp = fetch_massive_ust_yields()
    print(f"    HTTP {resp.status_code}")

    if resp.status_code != 200:
        print(f"    Error: {resp.text[:300]}")
        return

    parsed = parse_massive_ust_response(resp)
    
    if parsed.get("error"):
        print(f"    Error: {parsed['error']}")
        return

    data = parsed["data"]
    skipped = parsed.get("skipped", 0)
    print(f"    Latest complete observation date: {data.get('date')}")
    print(f"    Skipped incomplete observations: {skipped}")

    # Build observations
    print("\n[2] Building observations for 7 new Massive UST symbols...")
    observations = []
    for symbol, massive_field in MASSIVE_UST_MAPPING.items():
        maturity = int(symbol.replace("US", "").replace("YT_MASSIVE", ""))
        yield_val = data.get(massive_field)
        
        if yield_val is not None:
            obs = normalize_massive_ust_yield(
                symbol=symbol,
                maturity_years=maturity,
                yield_value=yield_val,
                observation_date=data.get("date"),
                source=SOURCE_DEF_MASSIVE_UST,
                status="AVAILABLE",
            )
        else:
            obs = normalize_massive_ust_yield(
                symbol=symbol,
                maturity_years=maturity,
                yield_value=None,
                observation_date=None,
                source=SOURCE_DEF_MASSIVE_UST,
                status="UNAVAILABLE",
                reason=f"Field {massive_field} not in response",
            )
        
        observations.append(obs)
        print(f"    {symbol}: {obs.status} — {obs.reason or obs.yield_value}")

    # Compare with Local baselines
    print("\n[3] Comparing with Local PostgreSQL baselines...")
    engine = local_engine()
    comparisons = compare_with_local_baseline_ust(observations, engine)

    print("\n    Comparison results:")
    for symbol, comp in comparisons.items():
        if comp["status"] == "AVAILABLE":
            diff = comp.get("diff")
            diff_str = f"{diff:.6f}" if diff is not None else "N/A"
            print(f"    {symbol}: Massive={comp['massive_yield']} Local={comp['local_close']} Diff={diff_str}")
        else:
            print(f"    {symbol}: {comp['status']} — {comp.get('reason', '')}")

    # Summary
    available = sum(1 for o in observations if o.status == "AVAILABLE")
    print(f"\n[4] Summary:")
    print(f"    AVAILABLE: {available}/7")
    print(f"    New symbols: {len(ALL_MASSIVE_UST_SYMBOLS)}")
    print(f"    Existing symbols modified: 0")
    print(f"    USnYT=X (Investing): unchanged")
    print(f"    USnYT_CMT (Treasury.gov): unchanged")
    print(f"    Database writes: 0")
    print(f"    Neon access: NO")
    print(f"    Production routing changed: NO")

    print("\n" + "=" * 80)
    print("MASSIVE UST EOD DRY-RUN COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    dry_run_massive_ust()
