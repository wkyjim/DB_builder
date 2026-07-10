from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine, neon_engine
from db_builder.eastmoney import fetch_and_save_all, fetch_and_save_tickers
from db_builder.indicators import daily_update_missing_indicators
from db_builder.neon_sync import daily_bulk_sync_to_neon
from db_builder.trading_calendar import (
    MIN_SESSION_COVERAGE_RATIO,
    database_latest_session_status,
)


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    local = local_engine(use_insertmanyvalues=True)
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    session_status = database_latest_session_status(local)
    latest_session = session_status.latest_session
    print(f"Latest completed NYSE session: {latest_session}")
    print(f"Database MAX(date): {session_status.max_date}")
    print(
        f"Session coverage: target={session_status.target_count:,} "
        f"baseline={session_status.baseline_count:,} "
        f"ratio={session_status.coverage_ratio:.2%} "
        f"required={MIN_SESSION_COVERAGE_RATIO:.0%}"
    )
    if session_status.should_skip and not args.force_refresh:
        print("[SKIP] Database already contains latest session")
        return
    if (
        session_status.max_date is not None
        and session_status.max_date >= latest_session
        and not session_status.should_skip
    ):
        print("[REFRESH] Latest session exists but ticker coverage is incomplete")

    if tickers:
        dry_run = args.dry_run or not args.upsert_local
        fetch_and_save_tickers(
            local,
            tickers,
            dry_run=dry_run,
            expected_session_date=latest_session,
            allow_non_trading_day=args.allow_non_trading_day,
        )
        daily_update_missing_indicators(
            local,
            dry_run=dry_run,
            limit=args.limit,
            tickers=tickers,
            allow_non_trading_day=args.allow_non_trading_day,
        )

        if args.upsert_local:
            print("Ticker-scoped local run complete; skipping Neon sync.")
            return
    else:
        fetch_and_save_all(
            local,
            dry_run=args.dry_run,
            limit=args.limit,
            expected_session_date=latest_session,
            allow_non_trading_day=args.allow_non_trading_day,
        )

    if args.fetch_only:
        print("[FETCH ONLY] Raw equity fetch complete; skipping indicators and Neon sync.")
        return

    daily_update_missing_indicators(
        local,
        dry_run=args.dry_run,
        limit=args.limit,
        allow_non_trading_day=args.allow_non_trading_day,
    )
    daily_bulk_sync_to_neon(
        local_engine=local,
        neon_engine=neon_engine(),
        dry_run=args.dry_run,
        limit=args.limit,
        allow_non_trading_day=args.allow_non_trading_day,
    )


if __name__ == "__main__":
    main()
