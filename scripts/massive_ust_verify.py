"""Massive UST 7-Tenor Final API Verification.

Makes exactly ONE API call, saves the complete raw response,
and analyzes all 10 returned observations for 7-tenor coverage.

Does NOT save or print API credentials.
"""
import sys
import os
import httpx
import time
import json
from datetime import datetime, timezone
from pathlib import Path

DB_BUILDER_ROOT = Path(r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder")
sys.path.insert(0, str(DB_BUILDER_ROOT / "src"))
from db_builder.config import local_engine

API_KEY = os.getenv("MASSIVE_API_KEY", "")

# Required tenor fields
REQUIRED_TENORS = [
    "yield_2_year", "yield_3_year", "yield_5_year",
    "yield_7_year", "yield_10_year", "yield_20_year", "yield_30_year",
]

# Save path for raw response
SAVE_PATH = Path(r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts\massive_ust_raw_response.json")

print("=" * 80)
print("MASSIVE UST 7-TENOR FINAL API VERIFICATION")
print("=" * 80)

# Make exactly ONE API call
print("\n[1] Making API call...")
print(f"    Endpoint: GET /fed/v1/treasury-yields?sort=date.desc&limit=10")

request_timestamp = datetime.now(timezone.utc).isoformat()
print(f"    Request timestamp (UTC): {request_timestamp}")

time.sleep(12)  # Rate limit compliance

resp = httpx.get(
    "https://api.polygon.io/fed/v1/treasury-yields",
    params={"apiKey": API_KEY, "sort": "date.desc", "limit": 10},
    timeout=30,
)

print(f"    HTTP status: {resp.status_code}")
print(f"    Response timestamp (UTC): {datetime.now(timezone.utc).isoformat()}")

if resp.status_code != 200:
    print(f"    ERROR: HTTP {resp.status_code}")
    print(f"    Response: {resp.text[:500]}")
    sys.exit(1)

# Parse response
data = resp.json()
print(f"    Top-level keys: {list(data.keys())}")

# Extract metadata
request_id = data.get("request_id", "N/A")
status = data.get("status", "N/A")
results = data.get("results", [])
next_url = data.get("next_url", None)

print(f"    Status: {status}")
print(f"    Request ID: {request_id}")
print(f"    Results count: {len(results)}")
print(f"    Next URL: {'Yes' if next_url else 'No'}")

# Save complete raw response (without credentials)
raw_artifact = {
    "http_status": resp.status_code,
    "request_timestamp_utc": request_timestamp,
    "response_timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "request_id": request_id,
    "status": status,
    "results_count": len(results),
    "has_next_url": next_url is not None,
    "results": results,
}

# Save to file
with open(SAVE_PATH, 'w', encoding='utf-8') as f:
    json.dump(raw_artifact, f, indent=2, ensure_ascii=False)

print(f"\n[2] Raw response saved to: {SAVE_PATH}")
print(f"    File size: {SAVE_PATH.stat().st_size} bytes")

# Analyze all 10 observations for 7-tenor coverage
print(f"\n[3] Analyzing {len(results)} observations for 7-tenor coverage...")

# Build coverage matrix
coverage_matrix = {}
tenor_stats = {t: {"VALID_NUMERIC": 0, "PRESENT_NULL": 0, "ABSENT": 0, "INVALID": 0} for t in REQUIRED_TENORS}

for obs in results:
    date = obs.get("date", "UNKNOWN")
    coverage_matrix[date] = {}
    
    for tenor in REQUIRED_TENORS:
        if tenor not in obs:
            classification = "ABSENT"
            coverage_matrix[date][tenor] = classification
            tenor_stats[tenor]["ABSENT"] += 1
        elif obs[tenor] is None:
            classification = "PRESENT_NULL"
            coverage_matrix[date][tenor] = classification
            tenor_stats[tenor]["PRESENT_NULL"] += 1
        elif isinstance(obs[tenor], (int, float)) and obs[tenor] > 0:
            classification = "VALID_NUMERIC"
            coverage_matrix[date][tenor] = {
                "classification": classification,
                "value": obs[tenor],
            }
            tenor_stats[tenor]["VALID_NUMERIC"] += 1
        else:
            classification = "INVALID"
            coverage_matrix[date][tenor] = {
                "classification": classification,
                "value": obs[tenor],
            }
            tenor_stats[tenor]["INVALID"] += 1

# Print coverage matrix
print(f"\n    Coverage Matrix (date x tenor):")
print(f"    {'Date':<12} {'2Y':<6} {'3Y':<6} {'5Y':<6} {'7Y':<6} {'10Y':<6} {'20Y':<6} {'30Y':<6}")
print(f"    {'-'*12} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")

for obs in results:
    date = obs.get("date", "UNKNOWN")
    row = f"    {date:<12}"
    for tenor in REQUIRED_TENORS:
        val = coverage_matrix[date][tenor]
        if isinstance(val, dict):
            cls = val["classification"]
            if cls == "VALID_NUMERIC":
                row += f" {'✓':<6}"
            elif cls == "PRESENT_NULL":
                row += f" {'null':<6}"
            else:
                row += f" {'✗':<6}"
        else:
            row += f" {'ABS':<6}"
    print(row)

# Print tenor statistics
print(f"\n    Tenor Coverage Statistics:")
for tenor in REQUIRED_TENORS:
    stats = tenor_stats[tenor]
    print(f"    {tenor}: VALID={stats['VALID_NUMERIC']}, NULL={stats['PRESENT_NULL']}, ABSENT={stats['ABSENT']}, INVALID={stats['INVALID']}")

# Task 3: Determine latest EOD dates
print(f"\n[4] Determining latest EOD dates...")

# Latest available date (first in desc-sorted results)
latest_available_date = results[0].get("date") if results else None
print(f"    LATEST_AVAILABLE_EOD_DATE: {latest_available_date}")

# Latest complete 7-tenor EOD date
latest_complete_date = None
for obs in results:
    date = obs.get("date")
    all_valid = True
    for tenor in REQUIRED_TENORS:
        if tenor not in obs or obs[tenor] is None or not isinstance(obs[tenor], (int, float)) or obs[tenor] <= 0:
            all_valid = False
            break
    if all_valid:
        latest_complete_date = date
        break

print(f"    LATEST_COMPLETE_7_TENOR_EOD_DATE: {latest_complete_date if latest_complete_date else 'NONE'}")

# Valid fields for latest date
if latest_available_date:
    latest_obs = results[0]
    valid_count = 0
    for tenor in REQUIRED_TENORS:
        if tenor in latest_obs and latest_obs[tenor] is not None and isinstance(latest_obs[tenor], (int, float)) and latest_obs[tenor] > 0:
            valid_count += 1
    print(f"    LATEST_DATE_VALID_FIELDS: {valid_count}/7")
else:
    valid_count = 0
    print(f"    LATEST_DATE_VALID_FIELDS: 0/7")

# Task 4: Diagnose missing tenors
print(f"\n[5] Diagnosing missing tenors...")

missing_tenors = []
for tenor in REQUIRED_TENORS:
    stats = tenor_stats[tenor]
    if stats["VALID_NUMERIC"] == 0 and stats["PRESENT_NULL"] == 0 and stats["INVALID"] == 0:
        # All ABSENT
        missing_tenors.append((tenor, "SOURCE_RESPONSE_MISSING (all 10 records)"))
    elif stats["VALID_NUMERIC"] == 0 and stats["PRESENT_NULL"] > 0:
        missing_tenors.append((tenor, "ALL_NULL"))
    elif stats["VALID_NUMERIC"] < len(results):
        missing_tenors.append((tenor, f"PARTIAL: {stats['VALID_NUMERIC']}/{len(results)} valid"))

if missing_tenors:
    print(f"    Missing/Problematic tenors:")
    for tenor, cause in missing_tenors:
        print(f"      {tenor}: {cause}")
else:
    print(f"    All 7 tenors have valid data in at least one observation")

# Root cause classification
root_cause = "UNVERIFIED"
if all(tenor_stats[t]["ABSENT"] == len(results) for t in ["yield_3_year", "yield_7_year", "yield_20_year"]):
    root_cause = "SOURCE_RESPONSE_MISSING (verified in 10-record window)"
elif all(tenor_stats[t]["VALID_NUMERIC"] == 0 for t in ["yield_3_year", "yield_7_year", "yield_20_year"]):
    root_cause = "ALL_NULL_OR_MISSING"
else:
    root_cause = "PARTIAL_COVERAGE"

print(f"    MISSING_TENOR_ROOT_CAUSE: {root_cause}")

# Final summary
print(f"\n{'='*80}")
print(f"FINAL VERIFICATION SUMMARY")
print(f"{'='*80}")
print(f"    MASSIVE_API_CALLS = 1")
print(f"    RAW_RESPONSE_SAVED = YES")
print(f"    RESPONSE_RECORDS = {len(results)}")
print(f"    LATEST_AVAILABLE_EOD_DATE = {latest_available_date}")
print(f"    LATEST_COMPLETE_7_TENOR_EOD_DATE = {latest_complete_date if latest_complete_date else 'NONE'}")
print(f"    LATEST_DATE_VALID_FIELDS = {valid_count}/7")
print(f"    2Y_COVERAGE = {tenor_stats['yield_2_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    3Y_COVERAGE = {tenor_stats['yield_3_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    5Y_COVERAGE = {tenor_stats['yield_5_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    7Y_COVERAGE = {tenor_stats['yield_7_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    10Y_COVERAGE = {tenor_stats['yield_10_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    20Y_COVERAGE = {tenor_stats['yield_20_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    30Y_COVERAGE = {tenor_stats['yield_30_year']['VALID_NUMERIC']}/{len(results)}")
print(f"    MISSING_TENOR_ROOT_CAUSE = {root_cause}")
print(f"    DATABASE_WRITES = 0")
print(f"    NEON_ACCESSED = NO")
print(f"    PRODUCTION_ROUTING_CHANGED = NO")
print(f"    READY_FOR_CONTROLLED_WRITE = NO")

print(f"\n{'='*80}")
print(f"VERIFICATION COMPLETE — 1 API CALL MADE")
print(f"{'='*80}")
