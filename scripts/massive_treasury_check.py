"""Query Massive Treasury Yields and reference tickers."""
import sys
import os
import httpx
import json

DB_BUILDER_ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
sys.path.insert(0, os.path.join(DB_BUILDER_ROOT, "src"))
from db_builder.config import local_engine  # Triggers load_external_env()

API_KEY = os.getenv("MASSIVE_API_KEY", "")
if not API_KEY:
    print("ERROR: MASSIVE_API_KEY not configured")
    sys.exit(1)

import time

def massive_get(endpoint, params=None):
    if params is None:
        params = {}
    params["apiKey"] = API_KEY
    time.sleep(12)  # 5 requests/minute
    resp = httpx.get(f"https://api.polygon.io{endpoint}", params=params, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Massive API error: {resp.status_code} - {resp.text[:300]}")
    return resp.json()

# Step 1: Treasury Yields
print("=" * 80)
print("MASSIVE TREASURY YIELDS ECONOMY API")
print("=" * 80)

try:
    treasury_data = massive_get("/fed/v1/treasury-yields", {"limit": 5})
    results = treasury_data.get("results", [])
    print(f"Results count: {len(results)}")
    if results:
        print(f"Latest date: {results[0].get('date')}")
        for r in results[:2]:
            print(f"  Date: {r.get('date')}")
            print(f"    2Y: {r.get('year2')}")
            print(f"    3Y: {r.get('year3')}")
            print(f"    5Y: {r.get('year5')}")
            print(f"    7Y: {r.get('year7')}")
            print(f"    10Y: {r.get('year10')}")
            print(f"    20Y: {r.get('year20')}")
            print(f"    30Y: {r.get('year30')}")
            
            # Check which maturities are available
            available = []
            for year in ['year2', 'year3', 'year5', 'year7', 'year10', 'year20', 'year30']:
                if r.get(year) is not None:
                    available.append(year.replace('year', ''))
            print(f"  Available maturities: {available}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")

# Step 2: Search reference tickers for KOSPI 200
print("\n" + "=" * 80)
print("MASSIVE REFERENCE TICKER SEARCH")
print("=" * 80)

search_terms = ["KOSPI", "Korea 200", "KS200", "DXY", "Dollar Index", "MOVE", "SKEW"]
results_map = {}

for term in search_terms:
    try:
        data = massive_get("/v3/reference/tickers", {"search": term, "limit": 5})
        ticks = data.get("results", [])
        print(f"\nSearch '{term}': {len(ticks)} results")
        for t in ticks[:3]:
            print(f"  {t.get('ticker')} | {t.get('name')} | market={t.get('market')}")
            results_map[term] = ticks
    except Exception as e:
        print(f"\nSearch '{term}': ERROR - {type(e).__name__}: {e}")
