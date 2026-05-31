from __future__ import annotations

import os
import sys

import requests
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import RAW_TABLE, local_engine, neon_engine
from db_builder.trading_calendar import fetch_max_equity_date
from db_builder.trading_calendar import latest_completed_nyse_session_date


DEFAULT_API_HEALTH_URL = "https://postgresql-us-equities-api.onrender.com/"


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
    url = os.getenv("API_HEALTH_URL", DEFAULT_API_HEALTH_URL)

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        print(f"[OK] API health endpoint: {url} | status={response.status_code}")
        return True
    except Exception as exc:
        print(f"[FAIL] API health endpoint: {url} | {exc}")
        return False


def main() -> int:
    local = local_engine()
    neon = neon_engine()

    checks = [
        check_database_connection("PostgreSQL", local),
        check_database_connection("Neon", neon),
        check_latest_date("local", local),
        check_latest_date("Neon", neon),
        check_latest_nyse_session(),
        check_api_health(),
    ]

    if all(checks):
        print("[OK] health check passed")
        return 0

    print("[FAIL] health check failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())

