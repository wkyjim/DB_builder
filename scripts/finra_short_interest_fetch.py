from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.finra_short_interest import (
    discover_short_interest_settlement_dates,
    refresh_short_interest_features,
    run_finra_short_interest_fetch,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch FINRA twice-monthly short-interest positions.")
    parser.add_argument("--dates", default="", help="Comma-separated settlement dates, YYYY-MM-DD.")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--five-year-backfill", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-existing", action="store_true")
    parser.add_argument("--refresh-analytics", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--massive-timeout", type=int, default=60)
    parser.add_argument("--no-massive-fallback", action="store_true")
    return parser.parse_args()


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def selected_dates(args: argparse.Namespace) -> list[date]:
    if args.dates:
        return [_date(item.strip()) for item in args.dates.split(",") if item.strip()]
    today = _date(args.end_date) if args.end_date else datetime.now(timezone.utc).date()
    if args.five_year_backfill:
        start = today - timedelta(days=5 * 366)
    elif args.start_date:
        start = _date(args.start_date)
    else:
        start = today - timedelta(days=75)
    return discover_short_interest_settlement_dates(start, today)


def main() -> None:
    args = parse_args()
    dates = selected_dates(args)
    print(f"[DATES] count={len(dates):,} start={min(dates, default='n/a')} end={max(dates, default='n/a')}")
    engine = local_engine(use_insertmanyvalues=True)
    result = run_finra_short_interest_fetch(
        engine,
        settlement_dates=dates,
        dry_run=args.dry_run or not args.upsert_local,
        skip_existing=not args.refresh_existing,
        timeout=args.timeout,
        massive_timeout=args.massive_timeout,
        massive_fallback=not args.no_massive_fallback,
        progress=lambda message: print(message, flush=True),
    )
    print(
        f"[FINRA SI] rows={result['rows']:,} upserted={result['upserted']:,} "
        f"latest={result['latest_available_date']} success_dates={len(result['successful_dates'])} "
        f"missing={len(result['missing_dates'])} failed={len(result['failed_dates'])} "
        f"skipped={len(result['skipped_dates'])} fallback_dates={len(result['fallback_dates'])} "
        f"fallback_rows={result['fallback_rows']:,} fallback_failed={len(result['fallback_failures'])}"
    )
    if args.refresh_analytics and not args.dry_run and args.upsert_local:
        refresh_short_interest_features(engine)


if __name__ == "__main__":
    main()
