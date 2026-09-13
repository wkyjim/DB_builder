from __future__ import annotations

import argparse
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.finra_short_analytics import refresh_daily_short_volume_features, setup_short_analytics_schema
from db_builder.finra_short_interest import refresh_short_interest_features
from db_builder.short_pipeline import latest_short_analytics, refresh_short_analytics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build point-in-time FINRA short analytics.")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--start-after-ticker", default=None, help="Resume final analytics after this ticker.")
    parser.add_argument("--skip-daily-features", action="store_true")
    parser.add_argument("--skip-si-features", action="store_true")
    parser.add_argument("--skip-intervals", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    setup_short_analytics_schema(engine)
    if args.summary:
        frame = latest_short_analytics(engine, limit=args.limit)
        print(frame.to_string(index=False) if not frame.empty else "No short analytics rows found.")
        return
    start = _date(args.start_date)
    end = _date(args.end_date) if args.end_date else datetime.now(timezone.utc).date()
    if not args.skip_daily_features:
        refresh_daily_short_volume_features(engine, output_start_date=start, end_date=end)
    if not args.skip_si_features:
        refresh_short_interest_features(engine)
    result = refresh_short_analytics(
        engine,
        start_date=start,
        end_date=end,
        batch_size=args.batch_size,
        build_intervals=not args.skip_intervals,
        start_after_ticker=args.start_after_ticker,
    )
    print(f"[COMPLETE] {result}")
    summary = latest_short_analytics(engine, limit=args.limit)
    print(summary.to_string(index=False) if not summary.empty else "No short analytics rows found.")


if __name__ == "__main__":
    main()
