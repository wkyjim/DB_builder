from __future__ import annotations

import argparse
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.cot_positions import latest_cot_summary, run_cot_fetch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch CFTC COT positioning into local PostgreSQL.")
    parser.add_argument("--years", default="", help="Comma-separated years. Default=current year.")
    parser.add_argument("--report-type", choices=["financial", "disaggregated"], default="financial")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args()


def _years(value: str) -> list[int] | None:
    if not value:
        return [datetime.now(timezone.utc).year]
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    if args.summary:
        summary = latest_cot_summary(engine)
        print(summary.to_string(index=False) if not summary.empty else "No local COT rows found.")
        return
    result = run_cot_fetch(
        engine,
        years=_years(args.years),
        report_type=args.report_type,
        dry_run=args.dry_run or not args.upsert_local,
        timeout=args.timeout,
    )
    if args.dry_run or not args.upsert_local:
        print(f"[DRY RUN] rows={result['rows']:,} latest={result['latest_available_date']} no database writes")
        return
    print(f"[UPSERT LOCAL] rows={result['upserted']:,} latest={result['latest_available_date']}")
    summary = latest_cot_summary(engine)
    print(summary.to_string(index=False) if not summary.empty else "No local COT rows found.")


if __name__ == "__main__":
    main()

