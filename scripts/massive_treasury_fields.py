"""Check Massive Treasury Yields field names."""
import sys
import os
import httpx
import json

DB_BUILDER_ROOT = r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
sys.path.insert(0, os.path.join(DB_BUILDER_ROOT, "src"))
from db_builder.config import local_engine

API_KEY = os.getenv("MASSIVE_API_KEY", "")
import time

def massive_get(endpoint, params=None):
    if params is None:
        params = {}
    params["apiKey"] = API_KEY
    time.sleep(12)
    resp = httpx.get(f"https://api.polygon.io{endpoint}", params=params, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Massive API error: {resp.status_code} - {resp.text[:300]}")
    return resp.json()

# Get latest with explicit date
try:
    data = massive_get("/fed/v1/treasury-yields", {"limit": 1})
    results = data.get("results", [])
    if results:
        print("Latest result keys:", list(results[0].keys()))
        print("Latest result:", json.dumps(results[0], indent=2))
    else:
        print("No results")
except Exception as e:
    print(f"ERROR: {e}")

# Try with different params
try:
    data = massive_get("/fed/v1/treasury-yields", {"limit": 3, "sort": "date", "order": "desc"})
    results = data.get("results", [])
    if results:
        print("\nWith sort=date, order=desc:")
        print("Keys:", list(results[0].keys()))
        print(json.dumps(results[0], indent=2))
except Exception as e:
    print(f"ERROR with sort: {e}")
