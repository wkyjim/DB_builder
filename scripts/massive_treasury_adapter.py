"""Massive Treasury Yield adapter for USnYT=X symbols (read-only dry-run).

Endpoint: GET /fed/v1/treasury-yields
Returns: yield_1_year, yield_5_year, yield_10_year (latest observation)

Mapping:
  US5YT=X  -> yield_5_year
  US10YT=X -> yield_10_year
  US2YT=X, US3YT=X, US7YT=X, US20YT=X, US30YT=X -> NOT AVAILABLE

All 7 USnYT_CMT symbols remain exclusively owned by Treasury.gov.
"""
import sys
import os
import httpx
import time
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

DB_BUILDER_ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
sys.path.insert(0, os.path.join(DB_BUILDER_ROOT, "src"))
from db_builder.config import local_engine

API_KEY = os.getenv("MASSIVE_API_KEY", "")

# Hermes symbol -> Massive field name
TREASURY_MAPPING: Dict[str, str] = {
    "US5YT=X": "yield_5_year",
    "US10YT=X": "yield_10_year",
}

# Symbols that Massive cannot provide
UNAVAILABLE_SYMBOLS = ["US2YT=X", "US3YT=X", "US7YT=X", "US20YT=X", "US30YT=X"]

# All 7 USnYT=X symbols
ALL_USNYTX = ["US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X"]

# All 7 USnYT_CMT symbols (remain Treasury.gov)
ALL_USNYT_CMT = ["US2YT_CMT", "US3YT_CMT", "US5YT_CMT", "US7YT_CMT", "US10YT_CMT", "US20YT_CMT", "US30YT_CMT"]

SOURCE_DEF_MASSIVE_TREASURY = "MASSIVE_ECONOMY_TREASURY_YIELD_DAILY"


@dataclass
class TreasuryObservation:
    symbol: str
    maturity_years: int
    yield_value: Optional[float]
    observation_date: Optional[str]
    source: str
    status: str
    reason: Optional[str] = None


def fetch_treasury_yields() -> Dict[str, Any]:
    """Fetch latest Treasury yields from Massive Economy API."""
    endpoint = "/fed/v1/treasury-yields"
    params = {"apiKey": API_KEY}
    time.sleep(12)  # Rate limit: 12s between requests
    resp = httpx.get(f"https://api.polygon.io{endpoint}", params=params, timeout=30)
    return resp


def parse_treasury_response(resp) -> Dict[str, Any]:
    """Parse Massive Treasury Yield response."""
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "data": None}
    return resp.json()


def normalize_treasury_yield(
    symbol: str,
    maturity_years: int,
    yield_value: Optional[float],
    observation_date: Optional[str],
    source: str,
    status: str,
    reason: Optional[str] = None,
) -> TreasuryObservation:
    """Normalize Treasury yield to Hermes schema."""
    return TreasuryObservation(
        symbol=symbol,
        maturity_years=maturity_years,
        yield_value=yield_value,
        observation_date=observation_date,
        source=source,
        status=status,
        reason=reason,
    )


def compare_with_local_baseline(
    observations: list,
    engine,
) -> Dict[str, Any]:
    """Compare Massive Treasury yields with Local PostgreSQL baselines."""
    import sqlalchemy as sa
    from sqlalchemy import text

    results = {}
    with engine.connect() as conn:
        for obs in observations:
            if obs.status != "AVAILABLE":
                results[obs.symbol] = {"status": obs.status, "reason": obs.reason}
                continue

            query = text(
                "SELECT close, session_date FROM public.macro "
                "WHERE symbol = :symbol ORDER BY session_date DESC LIMIT 1"
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
                    "reason": "No local baseline found",
                }
    return results


def dry_run_treasury():
    """Execute read-only dry-run for Treasury Yield migration."""
    print("=" * 80)
    print("MASSIVE TREASURY YIELD ADAPTER — DRY RUN")
    print("=" * 80)

    # Fetch from Massive
    print("\n[1] Fetching Treasury yields from Massive...")
    resp = fetch_treasury_yields()
    print(f"    HTTP {resp.status_code}")

    if resp.status_code != 200:
        print(f"    Error: {resp.text[:300]}")
        return

    data = resp.json()
    print(f"    Response keys: {list(data.keys())}")

    # Extract yield values
    yields = {}
    for key in ["yield_1_year", "yield_5_year", "yield_10_year"]:
        if key in data:
            yields[key] = data[key]
            print(f"    {key}: {data[key]}")

    # Get observation date
    obs_date = data.get("date") or data.get("last_updated")
    print(f"    Observation date: {obs_date}")

    # Build observations
    print("\n[2] Building observations for 7 USnYT=X symbols...")
    observations = []
    for symbol in ALL_USNYTX:
        maturity = int(symbol.replace("US", "").replace("YT=X", ""))
        if symbol in TREASURY_MAPPING:
            massive_field = TREASURY_MAPPING[symbol]
            yield_val = yields.get(massive_field)
            obs = normalize_treasury_yield(
                symbol=symbol,
                maturity_years=maturity,
                yield_value=yield_val,
                observation_date=obs_date,
                source=SOURCE_DEF_MASSIVE_TREASURY,
                status="AVAILABLE" if yield_val is not None else "UNAVAILABLE",
                reason=None if yield_val is not None else f"Field {massive_field} not in response",
            )
        else:
            obs = normalize_treasury_yield(
                symbol=symbol,
                maturity_years=maturity,
                yield_value=None,
                observation_date=None,
                source=SOURCE_DEF_MASSIVE_TREASURY,
                status="NOT_AVAILABLE",
                reason="Massive does not provide this maturity",
            )
        observations.append(obs)
        print(f"    {symbol}: {obs.status} — {obs.reason or obs.yield_value}")

    # Compare with Local baselines
    print("\n[3] Comparing with Local PostgreSQL baselines...")
    engine = local_engine()
    comparisons = compare_with_local_baseline(observations, engine)

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
    not_available = sum(1 for o in observations if o.status == "NOT_AVAILABLE")
    print(f"\n[4] Summary:")
    print(f"    AVAILABLE: {available}/7")
    print(f"    NOT_AVAILABLE: {not_available}/7")
    print(f"    USnYT_CMT: {len(ALL_USNYT_CMT)} symbols remain Treasury.gov (unchanged)")
    print(f"    Database writes: 0")
    print(f"    Neon access: NO")
    print(f"    Production routing changed: NO")

    print("\n" + "=" * 80)
    print("TREASURY YIELD DRY-RUN COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    dry_run_treasury()
