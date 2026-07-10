from __future__ import annotations

import argparse
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.finra_short_volume import latest_finra_summary, run_finra_fetch
from db_builder.flow_sources import business_dates_between, recent_business_dates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch FINRA daily short-sale volume into local PostgreSQL.")
    parser.add_argument("--dates", default="", help="Comma-separated YYYY-MM-DD dates. Default=recent business days.")
    parser.add_argument("--start-date", default=None, help="Start date for business-day range, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=None, help="End date for business-day range, YYYY-MM-DD. Default=today UTC.")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--market", default="CNMS")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args()


def _parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _dates(value: str, days: int, start_date: str | None, end_date: str | None):
    if value:
        return [_parse_date(item.strip()) for item in value.split(",") if item.strip()]
    if start_date:
        end = _parse_date(end_date) if end_date else datetime.now(timezone.utc).date()
        return business_dates_between(_parse_date(start_date), end)
    return recent_business_dates(days)


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    if args.summary:
        summary = latest_finra_summary(engine)
        print(summary.to_string(index=False) if not summary.empty else "No local FINRA short-sale volume rows found.")
        return
    selected_dates = _dates(args.dates, args.days, args.start_date, args.end_date)
    print(f"[DATES] count={len(selected_dates):,} start={min(selected_dates, default='n/a')} end={max(selected_dates, default='n/a')}")
    result = run_finra_fetch(
        engine,
        dates=selected_dates,
        market=args.market,
        dry_run=args.dry_run or not args.upsert_local,
        timeout=args.timeout,
    )
    if args.dry_run or not args.upsert_local:
        print(f"[DRY RUN] rows={result['rows']:,} latest={result['latest_available_date']} no database writes")
        return
    print(f"[UPSERT LOCAL] rows={result['upserted']:,} latest={result['latest_available_date']}")
    summary = latest_finra_summary(engine)
    print(summary.to_string(index=False) if not summary.empty else "No local FINRA short-sale volume rows found.")


if __name__ == "__main__":
    main()
