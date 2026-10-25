from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

import _bootstrap  # noqa: F401

from db_builder.config import INDICATOR_TABLE, RAW_TABLE, local_engine, neon_engine
from db_builder.eastmoney import fetch_and_save_all, fetch_and_save_tickers
from db_builder.indicators import daily_update_missing_indicators
from db_builder.massive_equity import fetch_and_save_session
from db_builder.neon_sync import daily_bulk_sync_to_neon
from db_builder.trading_calendar import (
    MIN_SESSION_COVERAGE_RATIO,
    database_latest_session_status,
    latest_completed_nyse_session_date,
)
from sqlalchemy import text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch equities, update indicators, and sync to Neon."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview each stage without writes.")
    parser.add_argument("--limit", type=int, default=None, help="Limit rows/tickers processed in dry-runs.")
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker subset, e.g. AA,AAPL.")
    parser.add_argument(
        "--upsert-local",
        action="store_true",
        help="Allow local raw/indicator upserts for ticker-scoped runs.",
    )
    parser.add_argument("--force-refresh", action="store_true", help="Bypass latest-session skip logic.")
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch raw equities only; indicator generation and Neon sync run separately.",
    )
    parser.add_argument(
        "--allow-non-trading-day",
        action="store_true",
        help="Debug only: allow non-NYSE trading dates through validation.",
    )
    parser.add_argument(
        "--source",
        choices=("massive", "eastmoney"),
        default="massive",
        help=(
            "Raw equity provider. 'massive' (default) uses the Polygon grouped-daily "
            "endpoint: one call per session for the whole US market. 'eastmoney' keeps "
            "the legacy paginated clist path."
        ),
    )
    return parser.parse_args()


def get_latest_stored_equity_session(engine) -> date | None:
    """Query the local PostgreSQL raw table for MAX(date)."""
    sql = text(f"SELECT MAX(date) AS max_date FROM {RAW_TABLE}")
    with engine.begin() as conn:
        row = conn.execute(sql).fetchone()
    if row is None or row.max_date is None:
        return None
    return row.max_date


def get_latest_indicator_session(engine) -> date | None:
    """Query the local PostgreSQL indicator table for MAX(date)."""
    sql = text(f"SELECT MAX(date) AS max_date FROM {INDICATOR_TABLE}")
    with engine.begin() as conn:
        row = conn.execute(sql).fetchone()
    if row is None or row.max_date is None:
        return None
    return row.max_date


def get_cycle_metadata(
    local,
    latest_completed: date | None,
    latest_stored: date | None,
    latest_indicator: date | None,
    force_refresh: bool = False,
) -> dict:
    """Build cycle metadata dict for equity session state.

    Fields:
        latest_completed_equity_session: The NYSE trading-calendar latest completed session.
        latest_stored_equity_session: The MAX(date) in the raw equities table.
        latest_indicator_session: The MAX(date) in the indicators table.
        equity_refresh_status: 'UPDATED' or 'SKIPPED_ALREADY_CURRENT'.
        indicator_refresh_status: 'UPDATED' or 'SKIPPED_ALREADY_CURRENT'.
        decision: The action taken.
    """
    equity_current = (
        latest_stored is not None
        and latest_completed is not None
        and latest_stored >= latest_completed
    )
    indicator_current = (
        latest_indicator is not None
        and latest_completed is not None
        and latest_indicator >= latest_completed
    )

    if force_refresh:
        decision = "force_refresh"
        equity_status = "UPDATED"
        indicator_status = "UPDATED"
    elif equity_current and indicator_current:
        decision = "skip_both"
        equity_status = "SKIPPED_ALREADY_CURRENT"
        indicator_status = "SKIPPED_ALREADY_CURRENT"
    elif not equity_current and indicator_current:
        decision = "indicators_only"
        equity_status = "UPDATED"
        indicator_status = "SKIPPED_ALREADY_CURRENT"
    elif equity_current and not indicator_current:
        decision = "equities_only"
        equity_status = "SKIPPED_ALREADY_CURRENT"
        indicator_status = "UPDATED"
    else:
        decision = "fetch_equities_and_indicators"
        equity_status = "UPDATED"
        indicator_status = "UPDATED"

    return {
        "latest_completed_equity_session": latest_completed.isoformat() if latest_completed else None,
        "latest_stored_equity_session": latest_stored.isoformat() if latest_stored else None,
        "latest_indicator_session": latest_indicator.isoformat() if latest_indicator else None,
        "equity_refresh_status": equity_status,
        "indicator_refresh_status": indicator_status,
        "decision": decision,
    }


def should_skip_equity_fetch(
    latest_completed: date | None,
    latest_stored: date | None,
    force_refresh: bool = False,
) -> bool:
    """Return True if the single-stock download should be skipped."""
    if force_refresh:
        return False
    if latest_completed is None or latest_stored is None:
        return False
    return latest_stored >= latest_completed


def should_skip_indicators(
    latest_completed: date | None,
    latest_indicator: date | None,
    force_refresh: bool = False,
) -> bool:
    """Return True if indicator processing should be skipped."""
    if force_refresh:
        return False
    if latest_completed is None or latest_indicator is None:
        return False
    return latest_indicator >= latest_completed


def main() -> None:
    args = parse_args()
    local = local_engine(use_insertmanyvalues=True)
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    # --- Determine latest completed NYSE session from trading calendar ---
    try:
        latest_completed = latest_completed_nyse_session_date()
    except RuntimeError as exc:
        print(f"[ERROR] Cannot determine latest completed NYSE session: {exc}")
        sys.exit(1)

    # --- Query local DB for stored sessions ---
    latest_stored = get_latest_stored_equity_session(local)
    latest_indicator = get_latest_indicator_session(local)

    print(f"Latest completed NYSE session (trading calendar): {latest_completed}")
    print(f"Latest stored equity session (DB MAX(date)):     {latest_stored}")
    print(f"Latest indicator session (DB MAX(date)):          {latest_indicator}")

    # --- Build cycle metadata ---
    metadata = get_cycle_metadata(
        local, latest_completed, latest_stored, latest_indicator,
        force_refresh=args.force_refresh,
    )
    print(f"Equity refresh status:   {metadata['equity_refresh_status']}")
    print(f"Indicator refresh status: {metadata['indicator_refresh_status']}")
    print(f"Decision:                {metadata['decision']}")

    # --- Case A: Both current, skip everything ---
    if metadata["decision"] == "skip_both" and not args.force_refresh:
        print("[SKIP] Equity prices and indicators already current through latest completed session.")
        print(f"[METADATA] {metadata}")
        return

    # --- Determine what needs to run ---
    needs_equity_fetch = metadata["decision"] in ("fetch_equities_and_indicators", "equities_only", "force_refresh")
    needs_indicators = metadata["decision"] in ("fetch_equities_and_indicators", "indicators_only", "force_refresh")

    # --- Fetch equities if needed ---
    if needs_equity_fetch:
        print(f"[FETCH] Fetching equity prices for session {latest_completed}")
        if tickers:
            dry_run = args.dry_run or not args.upsert_local
            fetch_and_save_tickers(
                local, tickers,
                dry_run=dry_run,
                expected_session_date=latest_completed,
                allow_non_trading_day=args.allow_non_trading_day,
            )
            if args.upsert_local:
                print("Ticker-scoped local run complete; skipping indicators and Neon sync.")
                return
        else:
            if args.source == "massive":
                print(f"[FETCH] source=massive (grouped daily) session {latest_completed}")
                fetch_and_save_session(
                    local,
                    latest_completed,
                    dry_run=args.dry_run,
                    allow_non_trading_day=args.allow_non_trading_day,
                )
            else:
                fetch_and_save_all(
                    local,
                    dry_run=args.dry_run,
                    limit=args.limit,
                    expected_session_date=latest_completed,
                    allow_non_trading_day=args.allow_non_trading_day,
                )
    else:
        print(f"[SKIP] Equity fetch not needed — stored session {latest_stored} >= completed {latest_completed}")

    # --- Update indicators if needed ---
    if needs_indicators and not args.fetch_only:
        print(f"[INDICATORS] Updating indicators through session {latest_completed}")
        daily_update_missing_indicators(
            local,
            dry_run=args.dry_run,
            limit=args.limit,
            allow_non_trading_day=args.allow_non_trading_day,
        )
    elif args.fetch_only:
        print("[FETCH ONLY] Raw equity fetch complete; skipping indicators.")
        return
    else:
        print(f"[SKIP] Indicator processing not needed — indicator session {latest_indicator} >= completed {latest_completed}")

    # --- Sync to Neon ---
    if not args.fetch_only:
        print("[NEON SYNC] Syncing to Neon...")
        daily_bulk_sync_to_neon(
            local_engine=local,
            neon_engine=neon_engine(),
            dry_run=args.dry_run,
            limit=args.limit,
            allow_non_trading_day=args.allow_non_trading_day,
        )

    print(f"[METADATA] {metadata}")
    print("[COMPLETE] Branch A finished successfully.")


if __name__ == "__main__":
    main()
