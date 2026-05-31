from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine, neon_engine
from db_builder.neon_sync import daily_bulk_sync_to_neon


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync local equities data to Neon.")
    parser.add_argument("--dry-run", action="store_true", help="Read only; do not upload or update sync_state.")
    parser.add_argument("--limit", type=int, default=None, help="Limit local rows previewed per table.")
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker subset, e.g. AA,AAPL.")
    parser.add_argument(
        "--allow-non-trading-day",
        action="store_true",
        help="Debug only: allow non-NYSE trading dates through validation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    daily_bulk_sync_to_neon(
        local_engine=local_engine(),
        neon_engine=neon_engine(),
        dry_run=args.dry_run,
        limit=args.limit,
        tickers=tickers,
        allow_non_trading_day=args.allow_non_trading_day,
    )


if __name__ == "__main__":
    main()
