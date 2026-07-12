from __future__ import annotations

import argparse
import json
from datetime import datetime

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.etf_flow.run import run_etf_flow_analytics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build deterministic ETF flow analytics tables and report output.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Calculate analytics without writing to PostgreSQL.")
    mode.add_argument("--upsert-local", action="store_true", help="Write ETF flow analytics to local PostgreSQL.")
    parser.add_argument("--as-of-date", help="Optional YYYY-MM-DD cutoff date.")
    parser.add_argument("--start-date", help="Optional YYYY-MM-DD start date for source history.")
    parser.add_argument("--existing-regime-score", type=float, help="Optional existing market regime score for combined regime output.")
    parser.add_argument("--write-report-output", action="store_true", help="Compatibility flag; analytics are persisted when --upsert-local is used.")
    parser.add_argument("--json", action="store_true", help="Print JSON output instead of a compact text summary.")
    return parser.parse_args()


def _parse_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    args = parse_args()
    dry_run = not args.upsert_local
    result = run_etf_flow_analytics(
        local_engine(),
        as_of_date=_parse_date(args.as_of_date),
        start_date=_parse_date(args.start_date),
        existing_regime_score=args.existing_regime_score,
        dry_run=dry_run,
        write_report_output=args.write_report_output,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    mode = "DRY RUN" if dry_run else "UPSERT LOCAL"
    print(f"[{mode}] ETF flow analytics as_of={result['as_of_date']}")
    print(
        f"raw={result['raw_rows']:,} daily={result['daily_rows']:,} "
        f"features={result['feature_rows']:,} segments={result['segment_rows']:,} "
        f"exposures={result.get('exposure_rows', 0):,}"
    )
    print(f"writes={result['writes']}")
    regime = result["output"].get("flow_regime") or {}
    print(
        "flow_regime="
        f"{regime.get('label')} score={regime.get('score')} confidence={regime.get('confidence')}"
    )
    for row in result["output"].get("exposures", [])[:8]:
        print(
            f"- {row.get('exposure_name')}: score={row.get('adjusted_flow_score'):.1f} "
            f"signal={row.get('flow_signal')} reliability={row.get('signal_reliability'):.1f} "
            f"status={row.get('data_availability_status')}"
        )


if __name__ == "__main__":
    main()
