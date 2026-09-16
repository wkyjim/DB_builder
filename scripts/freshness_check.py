"""
Post-ingestion freshness validation for DB Builder pipelines.

Read-only verification that each dataset has reached its expected freshness
target. Designed to run AFTER ingestion to validate that the pipeline
produced current data.

Exit codes:
    0 - All freshness checks passed (OK or non-critical WARN)
    1 - Deprecated (reserved for future use)
    2 - At least one FAIL threshold breached (action required)

In --require-critical mode:
    0 - No critical dataset FAIL
    2 - At least one critical dataset FAIL (non-critical FAILs are ignored)

Usage:
    python scripts/freshness_check.py
    python scripts/freshness_check.py --json
    python scripts/freshness_check.py --datasets equities,news
    python scripts/freshness_check.py --require-critical
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from db_builder.config import local_engine  # noqa: E402
from db_builder.trading_calendar import (  # noqa: E402
    NYSE,
    latest_completed_nyse_session_date,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FreshnessRule:
    """Declarative freshness rule for a single dataset."""

    name: str
    table: str
    timestamp_field: str
    is_date_not_timestamp: bool = True
    cadence: str = "daily_nyse"  # daily_nyse, daily_business, hourly, file
    warn_days: int = 2
    fail_days: int = 5
    is_critical: bool = False
    custom_query: str | None = None


@dataclass
class FreshnessResult:
    """Result of a single freshness check."""

    rule_name: str
    table: str
    timestamp_field: str
    latest_value: datetime | date | None
    lag_days: float | None
    status: str  # "OK", "WARN", "FAIL", "ERROR"
    message: str
    is_critical: bool


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def _count_nyse_sessions(start_date: date, end_date: date) -> int:
    """Count NYSE trading sessions between two dates (inclusive of end)."""
    if start_date >= end_date:
        return 0
    try:
        schedule = NYSE.schedule(start_date=start_date, end_date=end_date)
        return len(schedule)
    except Exception:
        # Fallback to calendar days
        return (end_date - start_date).days


def _count_business_days(start_date: date, end_date: date) -> int:
    """Count business days (Mon-Fri) between two dates."""
    if start_date >= end_date:
        return 0
    days = 0
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:  # Mon-Fri
            days += 1
        current += timedelta(days=1)
    return days


def _compute_lag_days(
    latest: datetime | date | None,
    *,
    is_date: bool,
    cadence: str,
) -> float | None:
    """Compute lag in business/trading days based on cadence."""
    if latest is None:
        return None

    now = datetime.now(timezone.utc)

    if is_date:
        if isinstance(latest, datetime):
            latest_date = latest.date()
        else:
            latest_date = latest

        if cadence == "daily_nyse":
            try:
                latest_session = latest_completed_nyse_session_date()
                return float(_count_nyse_sessions(latest_date, latest_session))
            except Exception:
                return max(0.0, float((now.date() - latest_date).days))
        elif cadence == "daily_business":
            return float(_count_business_days(latest_date, now.date()))
        else:
            return max(0.0, float((now.date() - latest_date).days))
    else:
        # Timestamp field
        if isinstance(latest, date) and not isinstance(latest, datetime):
            latest_dt = datetime.combine(latest, datetime.min.time(), tzinfo=timezone.utc)
        elif isinstance(latest, datetime):
            latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
        else:
            return None
        delta = now - latest_dt
        return max(0.0, delta.total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _fetch_latest_timestamp(
    rule: FreshnessRule,
    engine,
) -> datetime | date | None:
    """Execute the query and return the latest timestamp/date."""
    if rule.custom_query:
        query = rule.custom_query
    else:
        query = f"SELECT MAX({rule.timestamp_field}) AS latest_value FROM {rule.table}"

    try:
        with engine.begin() as conn:
            result = conn.execute(text(query))
            row = result.fetchone()
            if row is None or row[0] is None:
                return None
            value = row[0]
            if isinstance(value, datetime):
                return value
            if isinstance(value, date):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return date.fromisoformat(value)
            return None
    except Exception as exc:
        raise RuntimeError(f"Query failed for {rule.table}: {exc}") from exc


def _fetch_file_mtime(pattern: str) -> datetime | None:
    """Fetch the most recent file mtime matching a glob pattern."""
    reports_dir = PROJECT_ROOT / "reports"
    if not reports_dir.exists():
        return None
    newest: datetime | None = None
    for path in reports_dir.glob(pattern):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if newest is None or mtime > newest:
                newest = mtime
        except OSError:
            continue
    return newest


def _evaluate_status(
    lag_days: float | None,
    *,
    warn_days: int,
    fail_days: int,
) -> str:
    if lag_days is None:
        return "FAIL"
    if lag_days >= fail_days:
        return "FAIL"
    if lag_days >= warn_days:
        return "WARN"
    return "OK"


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------

FRESHNESS_RULES: list[FreshnessRule] = [
    FreshnessRule(
        name="Equities (raw)",
        table="public.us_equities",
        timestamp_field="date",
        is_date_not_timestamp=True,
        cadence="daily_nyse",
        warn_days=1,
        fail_days=3,
        is_critical=True,
    ),
    FreshnessRule(
        name="Equities (indicators)",
        table="public.us_equities_indicators",
        timestamp_field="date",
        is_date_not_timestamp=True,
        cadence="daily_nyse",
        warn_days=1,
        fail_days=3,
        is_critical=True,
    ),
    FreshnessRule(
        name="Macro (close)",
        table="public.macro",
        timestamp_field="date",
        is_date_not_timestamp=True,
        cadence="daily_nyse",
        warn_days=1,
        fail_days=3,
        is_critical=True,
    ),
    FreshnessRule(
        name="Macro (live)",
        table="public.macro_live",
        timestamp_field="observed_at",
        is_date_not_timestamp=False,
        cadence="hourly",
        warn_days=0,
        fail_days=0,
        is_critical=False,
        custom_query="SELECT MAX(observed_at) FROM public.macro_live",
    ),
    FreshnessRule(
        name="ETF flows",
        table="public.etf_daily_data",
        timestamp_field="date",
        is_date_not_timestamp=True,
        cadence="daily_nyse",
        warn_days=2,
        fail_days=5,
        is_critical=False,
    ),
    FreshnessRule(
        name="FINRA short volume",
        table="public.finra_short_volume",
        timestamp_field="trade_date",
        is_date_not_timestamp=True,
        cadence="daily_business",
        warn_days=3,
        fail_days=5,
        is_critical=False,
    ),
    FreshnessRule(
        name="Short analytics latest",
        table="public.us_equities_short_analytics_latest",
        timestamp_field="snapshot_updated_at",
        is_date_not_timestamp=False,
        cadence="daily_nyse",
        warn_days=2,
        fail_days=5,
        is_critical=False,
        custom_query="SELECT MAX(snapshot_updated_at) FROM public.us_equities_short_analytics_latest",
    ),
    FreshnessRule(
        name="Neon sync",
        table="public.sync_state",
        timestamp_field="updated_at",
        is_date_not_timestamp=False,
        cadence="daily_nyse",
        warn_days=2,
        fail_days=5,
        is_critical=False,
        custom_query="SELECT MAX(updated_at) FROM public.sync_state",
    ),
    FreshnessRule(
        name="Market report",
        table="(file system)",
        timestamp_field="file mtime",
        is_date_not_timestamp=False,
        cadence="file",
        warn_days=2,
        fail_days=4,
        is_critical=False,
    ),
]


# ---------------------------------------------------------------------------
# Check execution
# ---------------------------------------------------------------------------


def check_rule(rule: FreshnessRule, engine) -> FreshnessResult:
    """Execute a single freshness check."""
    try:
        # File-based rule
        if rule.cadence == "file":
            latest = _fetch_file_mtime("rule_based_market_update_*.md")
            if latest is None:
                return FreshnessResult(
                    rule_name=rule.name,
                    table=rule.table,
                    timestamp_field=rule.timestamp_field,
                    latest_value=None,
                    lag_days=None,
                    status="FAIL",
                    message="No market reports found in reports/ directory",
                    is_critical=rule.is_critical,
                )
            lag_days = _compute_lag_days(latest, is_date=False, cadence=rule.cadence)
            status = _evaluate_status(lag_days, warn_days=rule.warn_days, fail_days=rule.fail_days)
            return FreshnessResult(
                rule_name=rule.name,
                table=rule.table,
                timestamp_field=rule.timestamp_field,
                latest_value=latest,
                lag_days=lag_days,
                status=status,
                message=f"Latest report: {latest.isoformat()}",
                is_critical=rule.is_critical,
            )

        # Database query rules
        latest = _fetch_latest_timestamp(rule, engine)

        if latest is None:
            return FreshnessResult(
                rule_name=rule.name,
                table=rule.table,
                timestamp_field=rule.timestamp_field,
                latest_value=None,
                lag_days=None,
                status="FAIL",
                message=f"No rows found in {rule.table} (MAX({rule.timestamp_field}) IS NULL)",
                is_critical=rule.is_critical,
            )

        # Special handling for macro_live (hourly cadence)
        if rule.name == "Macro (live)":
            now = datetime.now(timezone.utc)
            if isinstance(latest, date) and not isinstance(latest, datetime):
                latest_dt = datetime.combine(latest, datetime.min.time(), tzinfo=timezone.utc)
            elif isinstance(latest, datetime):
                latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            else:
                latest_dt = None

            if latest_dt is None:
                lag_hours = None
            else:
                lag_hours = (now - latest_dt).total_seconds() / 3600.0

            if lag_hours is None:
                status = "FAIL"
                message = "Could not parse observed_at timestamp"
            elif lag_hours >= 12:
                status = "FAIL"
                message = f"Macro live data is {lag_hours:.1f} hours old (threshold: 12h)"
            elif lag_hours >= 4:
                status = "WARN"
                message = f"Macro live data is {lag_hours:.1f} hours old (threshold: 4h)"
            else:
                status = "OK"
                message = f"Macro live data is {lag_hours:.1f} hours old"

            return FreshnessResult(
                rule_name=rule.name,
                table=rule.table,
                timestamp_field=rule.timestamp_field,
                latest_value=latest,
                lag_days=lag_hours / 24.0 if lag_hours is not None else None,
                status=status,
                message=message,
                is_critical=rule.is_critical,
            )

        # Standard date/timestamp evaluation
        lag_days = _compute_lag_days(
            latest,
            is_date=rule.is_date_not_timestamp,
            cadence=rule.cadence,
        )
        status = _evaluate_status(lag_days, warn_days=rule.warn_days, fail_days=rule.fail_days)

        if rule.is_date_not_timestamp:
            latest_str = str(latest)
        else:
            if isinstance(latest, datetime):
                latest_str = latest.isoformat()
            else:
                latest_str = str(latest)

        if status == "OK":
            message = f"{rule.name} is fresh: {latest_str}"
        elif status == "WARN":
            message = f"{rule.name} is {lag_days:.1f} days old (threshold: {rule.warn_days})"
        else:
            message = f"{rule.name} is {lag_days:.1f} days old (threshold: {rule.fail_days})"

        return FreshnessResult(
            rule_name=rule.name,
            table=rule.table,
            timestamp_field=rule.timestamp_field,
            latest_value=latest,
            lag_days=lag_days,
            status=status,
            message=message,
            is_critical=rule.is_critical,
        )

    except Exception as exc:
        return FreshnessResult(
            rule_name=rule.name,
            table=rule.table,
            timestamp_field=rule.timestamp_field,
            latest_value=None,
            lag_days=None,
            status="ERROR",
            message=f"Check failed: {exc}",
            is_critical=rule.is_critical,
        )


def check_all_rules(
    engine,
    datasets: list[str] | None = None,
) -> list[FreshnessResult]:
    """Execute all (or filtered) freshness checks."""
    rules = FRESHNESS_RULES
    if datasets:
        datasets_lower = {d.lower() for d in datasets}
        rules = [r for r in rules if r.name.lower() in datasets_lower or r.table.lower() in datasets_lower]
    return [check_rule(rule, engine) for rule in rules]


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def format_text(results: list[FreshnessResult]) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("DATA FRESHNESS CHECK")
    lines.append("=" * 72)
    lines.append("")

    ok_count = sum(1 for r in results if r.status == "OK")
    warn_count = sum(1 for r in results if r.status == "WARN")
    fail_count = sum(1 for r in results if r.status == "FAIL")
    error_count = sum(1 for r in results if r.status == "ERROR")

    for r in results:
        marker = {
            "OK": "[OK]",
            "WARN": "[WARN]",
            "FAIL": "[FAIL]",
            "ERROR": "[ERROR]",
        }.get(r.status, "[?]")

        critical_marker = " *CRITICAL*" if r.is_critical else ""
        lines.append(f"{marker} {r.rule_name}{critical_marker}")
        lines.append(f"       {r.message}")
        if r.lag_days is not None:
            lag_display = f"{r.lag_days:.1f} days" if r.lag_days >= 1 else f"{r.lag_days * 24:.1f} hours"
            lines.append(f"       Lag: {lag_display}")
        lines.append("")

    lines.append("-" * 72)
    lines.append(f"Summary: {ok_count} OK, {warn_count} WARN, {fail_count} FAIL, {error_count} ERROR")

    if fail_count:
        lines.append("")
        lines.append("FAILED datasets:")
        for r in results:
            if r.status == "FAIL":
                critical = " (CRITICAL)" if r.is_critical else ""
                lines.append(f"  - {r.rule_name}{critical}")

    if warn_count:
        lines.append("")
        lines.append("WARNING datasets:")
        for r in results:
            if r.status == "WARN":
                lines.append(f"  - {r.rule_name}")

    return "\n".join(lines)


def format_json(results: list[FreshnessResult]) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [
            {
                "rule_name": r.rule_name,
                "table": r.table,
                "timestamp_field": r.timestamp_field,
                "latest_value": str(r.latest_value) if r.latest_value else None,
                "lag_days": r.lag_days,
                "status": r.status,
                "message": r.message,
                "is_critical": r.is_critical,
            }
            for r in results
        ],
        "summary": {
            "ok": sum(1 for r in results if r.status == "OK"),
            "warn": sum(1 for r in results if r.status == "WARN"),
            "fail": sum(1 for r in results if r.status == "FAIL"),
            "error": sum(1 for r in results if r.status == "ERROR"),
        },
    }
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate data freshness after pipeline ingestion.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--datasets",
        type=str,
        default=None,
        help="Comma-separated list of dataset names to check (default: all).",
    )
    parser.add_argument(
        "--require-critical",
        action="store_true",
        help="Exit 2 if any critical dataset fails freshness.",
    )
    parser.add_argument(
        "--list-datasets",
        action="store_true",
        help="List available dataset rules and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_datasets:
        print("Available freshness rules:")
        for rule in FRESHNESS_RULES:
            critical = " *CRITICAL*" if rule.is_critical else ""
            print(f"  - {rule.name}{critical} ({rule.table}.{rule.timestamp_field})")
        return 0

    # Validate dataset names if provided
    if args.datasets:
        datasets_lower = {d.strip().lower() for d in args.datasets.split(",")}
        valid_names = {r.name.lower() for r in FRESHNESS_RULES}
        valid_tables = {r.table.lower() for r in FRESHNESS_RULES}
        valid_all = valid_names | valid_tables
        
        unknown = datasets_lower - valid_all
        if unknown:
            print(f"ERROR: Unknown dataset(s): {', '.join(sorted(unknown))}")
            print(f"Available datasets: {', '.join(sorted(valid_names))}")
            return 2

    # Create single engine shared across all rules
    engine = local_engine()

    try:
        datasets = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
        results = check_all_rules(engine, datasets=datasets)

        if args.json:
            print(format_json(results))
        else:
            print(format_text(results))

        # Determine exit code
        has_fail = any(r.status == "FAIL" for r in results)
        has_critical_fail = any(r.status == "FAIL" and r.is_critical for r in results)

        if args.require_critical:
            # In gating mode, only critical failures cause non-zero exit
            if has_critical_fail:
                return 2
            return 0
        else:
            # Default mode: any FAIL causes exit 2
            if has_fail:
                return 2
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
