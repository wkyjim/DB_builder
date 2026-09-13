from __future__ import annotations

import argparse
import os
import sys

import requests
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import RAW_TABLE, local_engine, neon_engine
from db_builder.trading_calendar import fetch_max_equity_date
from db_builder.trading_calendar import latest_completed_nyse_session_date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check equity-pipeline dependencies.")
    parser.add_argument(
        "--require-neon",
        action="store_true",
        help="Fail when Neon is unavailable. Local-first jobs leave this advisory.",
    )
    parser.add_argument(
        "--require-api",
        action="store_true",
        help="Fail when the configured public market API is unavailable.",
    )
    return parser.parse_args()


def check_database_connection(name: str, engine) -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
        print(f"[OK] {name} connection")
        return True
    except Exception as exc:
        print(f"[FAIL] {name} connection: {exc}")
        return False


def check_latest_date(name: str, engine) -> bool:
    try:
        latest_date = fetch_max_equity_date(engine, RAW_TABLE)
        print(f"[OK] latest {name} date: {latest_date}")
        return latest_date is not None
    except Exception as exc:
        print(f"[FAIL] latest {name} date: {exc}")
        return False


def check_latest_nyse_session() -> bool:
    try:
        latest_session = latest_completed_nyse_session_date()
        print(f"[OK] latest NYSE session: {latest_session}")
        return True
    except Exception as exc:
        print(f"[FAIL] latest NYSE session: {exc}")
        return False


def check_api_health() -> bool:
    url = (os.getenv("API_HEALTH_URL") or os.getenv("MARKET_API_BASE_URL") or "").rstrip("/")
    if not url:
        print("[FAIL] API health endpoint: MARKET_API_BASE_URL is not configured")
        return False

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        print(f"[OK] API health endpoint: {url} | status={response.status_code}")
        return True
    except Exception as exc:
        print(f"[FAIL] API health endpoint: {url} | {exc}")
        return False


def check_neon() -> bool:
    try:
        engine = neon_engine()
    except Exception as exc:
        print(f"[FAIL] Neon configuration: {type(exc).__name__}")
        return False
    return check_database_connection("Neon", engine) and check_latest_date("Neon", engine)


def main() -> int:
    args = parse_args()
    local = local_engine()
    required_checks = [
        check_database_connection("PostgreSQL", local),
        check_latest_date("local", local),
        check_latest_nyse_session(),
    ]

    neon_ok = check_neon()
    api_ok = check_api_health()
    if not neon_ok and not args.require_neon:
        print("[WARN] Neon preflight failed; local ingestion may continue and sync can retry later.")
    if not api_ok and not args.require_api:
        print("[WARN] Public API health failed; it does not block local ingestion.")
    if args.require_neon:
        required_checks.append(neon_ok)
    if args.require_api:
        required_checks.append(api_ok)

    if all(required_checks):
        print("[OK] health check passed")
        return 0

    print("[FAIL] health check failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
