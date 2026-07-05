from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.economic_data import (
    build_derived_inflation_rows,
    create_economic_indicators_table,
    default_release_calendar,
    fetch_fred_csv,
    latest_economic_summary,
    replace_upcoming_release_calendar,
    series_registry,
    should_fetch_series,
    upcoming_release_summary,
    upsert_economic_indicators,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch free economic indicator data into local PostgreSQL.")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--series", default="", help="Comma-separated FRED series IDs. Defaults to the full registry.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--release-calendar", action="store_true", help="Show upcoming release calendar rows.")
    parser.add_argument("--force-refresh", action="store_true", help="Fetch even if the next release time has not passed.")
    return parser.parse_args()


def _selected_series(series_arg: str):
    registry = series_registry()
    if not series_arg:
        return list(registry.values())
    selected = []
    for raw in series_arg.split(","):
        series_id = raw.strip().upper()
        if not series_id:
            continue
        if series_id not in registry:
            raise SystemExit(f"Unknown series ID: {series_id}")
        selected.append(registry[series_id])
    return selected


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    create_economic_indicators_table(engine)
    selected = _selected_series(args.series)
    calendar_count = replace_upcoming_release_calendar(engine, default_release_calendar())
    print(f"[RELEASE CALENDAR] upserted_rows={calendar_count:,}")

    if args.summary:
        summary = latest_economic_summary(engine, series_ids=[series.series_id for series in selected])
        print(summary.to_string(index=False) if not summary.empty else "No local economic indicator rows found.")
        return

    if args.release_calendar:
        calendar = upcoming_release_summary(engine, series_ids=[series.series_id for series in selected])
        print(calendar.to_string(index=False) if not calendar.empty else "No upcoming release calendar rows found.")
        return

    all_rows = []
    for series in selected:
        should_fetch, reason = should_fetch_series(engine, series.series_id, force_refresh=args.force_refresh)
        if not should_fetch:
            print(f"[SKIP] {series.series_id} {reason}")
            continue
        print(f"[DUE] {series.series_id} {reason}")
        rows = fetch_fred_csv(series, start_date=args.start_date)
        all_rows.extend(rows)
        latest = max((row["date"] for row in rows), default="n/a")
        print(f"[FETCHED] {series.series_id} rows={len(rows):,} latest={latest}")

    if args.dry_run:
        print(f"[DRY RUN] fetched_rows={len(all_rows):,}; no database writes")
        return

    if not args.upsert_local:
        print("[NO WRITE] pass --upsert-local to write fetched rows")
        return

    count = upsert_economic_indicators(engine, all_rows)
    print(f"[UPSERT LOCAL] rows={count:,}")
    derived_rows = build_derived_inflation_rows(engine, start_date=args.start_date)
    derived_count = upsert_economic_indicators(engine, derived_rows)
    print(f"[DERIVED INFLATION] rows={derived_count:,}")
    summary = latest_economic_summary(engine, series_ids=[series.series_id for series in selected])
    print(summary.to_string(index=False) if not summary.empty else "No local economic indicator rows found.")


if __name__ == "__main__":
    main()
