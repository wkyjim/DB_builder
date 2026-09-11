from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import INDICATOR_TABLE, RAW_TABLE, local_engine, neon_engine
from db_builder.neon_sync import daily_bulk_sync_to_neon


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync validated local equities data to Neon.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--overlap-days", type=int, default=5)
    parser.add_argument("--reconciliation-days", type=int, default=60)
    parser.add_argument(
        "--minimum-date",
        default=None,
        help="Never read or upload rows before this retention boundary (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--tables",
        choices=["all", "equities", "indicators"],
        default="all",
    )
    parser.add_argument("--allow-non-trading-day", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tickers = [value.strip().upper() for value in args.tickers.split(",")] if args.tickers else None
    table_map = {
        "all": None,
        "equities": [RAW_TABLE],
        "indicators": [INDICATOR_TABLE],
    }
    daily_bulk_sync_to_neon(
        local_engine=local_engine(),
        neon_engine=neon_engine(),
        tables=table_map[args.tables],
        overlap_days=args.overlap_days,
        reconciliation_days=args.reconciliation_days,
        dry_run=args.dry_run,
        limit=args.limit,
        tickers=tickers,
        allow_non_trading_day=args.allow_non_trading_day,
        minimum_date=args.minimum_date,
    )


if __name__ == "__main__":
    main()
