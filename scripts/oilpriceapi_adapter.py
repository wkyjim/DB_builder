"""OilPriceAPI adapter for Brent and WTI oil prices (read-only dry-run).

Endpoint: GET /v1/prices/latest?by_code=BRENT_CRUDE_USD
Auth: Authorization: Token <OIL_PRICE_API_KEY>

Returns spot/indicative prices, NOT futures settlement prices.
Does NOT provide OHLC or previous-close values.

Response structure:
{
    "data": {
        "code": "BRENT_CRUDE_USD",
        "price": 85.50,
        "currency": "USD",
        "unit": "barrel",
        "as_of": "2026-10-19T12:00:00Z",
        "created_at": "2026-10-19T12:00:00Z"
    }
}
"""
import sys
import os
import time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

import requests

DB_BUILDER_ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
sys.path.insert(0, os.path.join(DB_BUILDER_ROOT, "src"))
from db_builder.config import local_engine

OIL_PRICE_API_KEY = os.getenv("OIL_PRICE_API_KEY", "")

# New independent OilPriceAPI symbols -> OilPriceAPI commodity codes
OIL_MAPPING: Dict[str, str] = {
    "BRENT_OILPRICE": "BRENT_CRUDE_USD",
    "WTI_OILPRICE": "WTI_USD",
}

SOURCE_DEF_OILPRICEAPI = "OILPRICEAPI_SPOT_INDICATIVE_USD_PER_BARREL"


@dataclass
class OilPriceObservation:
    symbol: str
    commodity_code: str
    price: Optional[float]
    currency: Optional[str]
    unit: Optional[str]
    timestamp: Optional[str]
    source: str
    price_type: str
    status: str
    reason: Optional[str] = None


def fetch_oil_price(commodity_code: str) -> Dict[str, Any]:
    """Fetch oil price from OilPriceAPI for a single commodity code.

    Uses the exact endpoint format from the working reference implementation.
    """
    url = f"https://api.oilpriceapi.com/v1/prices/latest?by_code={commodity_code}"
    headers = {"Authorization": f"Token {OIL_PRICE_API_KEY}"}
    time.sleep(12)  # Rate limit: 12s between requests
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def parse_oil_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse OilPriceAPI response and extract price data."""
    price_data = data.get("data", {})
    return {
        "code": price_data.get("code"),
        "price": price_data.get("price"),
        "currency": price_data.get("currency"),
        "unit": price_data.get("unit"),
        "timestamp": price_data.get("as_of") or price_data.get("created_at"),
    }


def validate_oil_price(
    symbol: str,
    commodity_code: str,
    price: Optional[float],
    currency: Optional[str],
    unit: Optional[str],
    timestamp: Optional[str],
) -> OilPriceObservation:
    """Validate oil price observation."""
    source = SOURCE_DEF_OILPRICEAPI
    price_type = "spot_indicative"

    if price is None:
        return OilPriceObservation(
            symbol=symbol,
            commodity_code=commodity_code,
            price=None,
            currency=currency,
            unit=unit,
            timestamp=timestamp,
            source=source,
            price_type=price_type,
            status="MISSING",
            reason="No price in response",
        )

    if price <= 0:
        return OilPriceObservation(
            symbol=symbol,
            commodity_code=commodity_code,
            price=price,
            currency=currency,
            unit=unit,
            timestamp=timestamp,
            source=source,
            price_type=price_type,
            status="INVALID",
            reason=f"Non-positive price: {price}",
        )

    if currency and currency != "USD":
        return OilPriceObservation(
            symbol=symbol,
            commodity_code=commodity_code,
            price=price,
            currency=currency,
            unit=unit,
            timestamp=timestamp,
            source=source,
            price_type=price_type,
            status="INVALID",
            reason=f"Non-USD currency: {currency}",
        )

    return OilPriceObservation(
        symbol=symbol,
        commodity_code=commodity_code,
        price=price,
        currency=currency,
        unit=unit,
        timestamp=timestamp,
        source=source,
        price_type=price_type,
        status="AVAILABLE",
        reason=None,
    )


def compare_with_local_baseline_oil(
    observations: List[OilPriceObservation],
    engine,
) -> Dict[str, Any]:
    """Compare OilPriceAPI prices with Local PostgreSQL baselines."""
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
                diff = abs(obs.price - local_close) if obs.price and local_close else None
                pct_diff = (diff / local_close * 100) if diff and local_close else None
                results[obs.symbol] = {
                    "status": "AVAILABLE",
                    "oilpriceapi_price": obs.price,
                    "oilpriceapi_timestamp": obs.timestamp,
                    "local_close": local_close,
                    "local_date": local_date,
                    "abs_diff": diff,
                    "pct_diff": pct_diff,
                    "price_type": obs.price_type,
                    "note": "Spot/indicative vs futures settlement — not directly comparable",
                }
            else:
                results[obs.symbol] = {
                    "status": "NO_BASELINE",
                    "reason": "No local baseline found (new symbol)",
                }
    return results


def dry_run_oil():
    """Execute read-only dry-run for OilPriceAPI migration."""
    print("=" * 80)
    print("OILPRICEAPI ADAPTER — DRY RUN")
    print("=" * 80)

    # Fetch from OilPriceAPI
    print("\n[1] Fetching oil prices from OilPriceAPI...")
    observations = []

    for symbol, commodity_code in OIL_MAPPING.items():
        print(f"    Fetching {symbol} ({commodity_code})...")
        try:
            data = fetch_oil_price(commodity_code)
            parsed = parse_oil_response(data)
            obs = validate_oil_price(
                symbol=symbol,
                commodity_code=commodity_code,
                price=parsed.get("price"),
                currency=parsed.get("currency"),
                unit=parsed.get("unit"),
                timestamp=parsed.get("timestamp"),
            )
            observations.append(obs)
            print(f"    {symbol}: {obs.status} — {obs.reason or obs.price}")
        except Exception as e:
            obs = OilPriceObservation(
                symbol=symbol,
                commodity_code=commodity_code,
                price=None,
                currency=None,
                unit=None,
                timestamp=None,
                source=SOURCE_DEF_OILPRICEAPI,
                price_type="spot_indicative",
                status="FETCH_FAILED",
                reason=str(e),
            )
            observations.append(obs)
            print(f"    {symbol}: FETCH_FAILED — {e}")

    # Compare with Local baselines
    print("\n[2] Comparing with Local PostgreSQL baselines...")
    engine = local_engine()
    comparisons = compare_with_local_baseline_oil(observations, engine)

    print("\n    Comparison results:")
    for symbol, comp in comparisons.items():
        if comp["status"] == "AVAILABLE":
            print(f"    {symbol}: OilPriceAPI={comp['oilpriceapi_price']} Local={comp['local_close']}")
            print(f"      Abs diff: {comp['abs_diff']:.4f}, Pct diff: {comp['pct_diff']:.2f}%")
            print(f"      Note: {comp['note']}")
        else:
            print(f"    {symbol}: {comp['status']} — {comp.get('reason', '')}")

    # Summary
    available = sum(1 for o in observations if o.status == "AVAILABLE")
    print(f"\n[3] Summary:")
    print(f"    AVAILABLE: {available}/2")
    print(f"    Price type: Spot/indicative (NOT futures settlement)")
    print(f"    OHLC provided: NO")
    print(f"    Previous close provided: NO")
    print(f"    Database writes: 0")
    print(f"    Neon access: NO")
    print(f"    Production routing changed: NO")

    print("\n" + "=" * 80)
    print("OILPRICEAPI DRY-RUN COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    dry_run_oil()
