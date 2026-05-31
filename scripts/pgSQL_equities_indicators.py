from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.indicators import daily_update_missing_indicators


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update local equity indicators.")
    parser.add_argument("--dry-run", action="store_true", help="Calculate only; do not upsert.")
    parser.add_argument("--limit", type=int, default=None, help="Limit tickers processed.")
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker subset, e.g. AA,AAPL.")
    parser.add_argument(
        "--upsert-local",
        action="store_true",
        help="Allow local upserts for ticker-scoped runs.",
    )
    parser.add_argument(
        "--allow-non-trading-day",
        action="store_true",
        help="Debug only: allow non-NYSE trading dates through validation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    dry_run = args.dry_run or (bool(tickers) and not args.upsert_local)

    daily_update_missing_indicators(
        engine,
        dry_run=dry_run,
        limit=args.limit,
        tickers=tickers,
        allow_non_trading_day=args.allow_non_trading_day,
    )


if __name__ == "__main__":
    main()
