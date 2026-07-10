from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import INDICATOR_TABLE, RAW_TABLE, local_engine, neon_engine
from db_builder.trading_calendar import (
    MIN_SESSION_COVERAGE_RATIO,
    NYSE,
    latest_completed_nyse_session_date,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit equity coverage by NYSE session.")
    parser.add_argument("--start-date", default="2026-05-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--include-neon", action="store_true")
    parser.add_argument(
        "--coverage-universe",
        choices=["core", "all"],
        default="core",
        help="Core excludes non-price records, warrants, rights, units, preferreds, and debt.",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def fetch_keys(engine, table_name: str, start_date, end_date) -> pd.DataFrame:
    return pd.read_sql(
        text(
            f"""
            SELECT date, ticker
            FROM {table_name}
            WHERE date BETWEEN :start_date AND :end_date
            ORDER BY date, ticker
            """
        ),
        engine,
        params={"start_date": start_date, "end_date": end_date},
    )


def fetch_coverage_eligible_tickers(engine) -> list[str]:
    frame = pd.read_sql(
        text(
            """
            SELECT ticker
            FROM public.equity_security_status
            WHERE coverage_eligible = true
            ORDER BY ticker
            """
        ),
        engine,
    )
    return frame["ticker"].astype(str).tolist()


def fetch_raw_coverage_keys(
    engine,
    start_date,
    end_date,
    *,
    core_only: bool,
    eligible_tickers: list[str] | None = None,
) -> pd.DataFrame:
    if core_only:
        return pd.read_sql(
            text(
                f"""
                SELECT date, ticker
                FROM {RAW_TABLE}
                WHERE date BETWEEN :start_date AND :end_date
                  AND ticker = ANY(:eligible_tickers)
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY date, ticker
                """
            ),
            engine,
            params={
                "start_date": start_date,
                "end_date": end_date,
                "eligible_tickers": eligible_tickers or [],
            },
        )
    return pd.read_sql(
        text(
            f"""
            SELECT date, ticker
            FROM {RAW_TABLE}
            WHERE date BETWEEN :start_date AND :end_date
            ORDER BY date, ticker
            """
        ),
        engine,
        params={"start_date": start_date, "end_date": end_date},
    )


def keys_by_date(frame: pd.DataFrame) -> dict:
    result: dict = defaultdict(set)
    if frame.empty:
        return result
    for session_date, group in frame.groupby("date"):
        result[pd.Timestamp(session_date).date()] = set(group["ticker"].astype(str))
    return result


def expected_sessions(start_date, end_date) -> list:
    schedule = NYSE.schedule(start_date=start_date, end_date=end_date)
    return [timestamp.date() for timestamp in schedule.index]


def build_session_rows(
    sessions: list,
    raw_by_date: dict,
    indicator_by_date: dict,
    *,
    minimum_ratio: float = MIN_SESSION_COVERAGE_RATIO,
) -> list[dict]:
    rows: list[dict] = []
    populated_history: list[tuple[int, set[str]]] = []
    previous_tickers: set[str] = set()

    for session_date in sessions:
        raw_tickers = raw_by_date.get(session_date, set())
        indicator_tickers = indicator_by_date.get(session_date, set())
        baseline_count, baseline_tickers = max(
            populated_history[-5:],
            key=lambda item: item[0],
            default=(0, set()),
        )
        baseline = baseline_count
        ratio = len(raw_tickers) / baseline if baseline else 0.0
        missing_previous = previous_tickers - raw_tickers if previous_tickers else set()
        missing_baseline = baseline_tickers - raw_tickers if baseline_tickers else set()
        new_vs_previous = raw_tickers - previous_tickers if previous_tickers else set()
        missing_indicators = raw_tickers - indicator_tickers
        flags = []
        if not raw_tickers:
            flags.append("MISSING_SESSION")
        elif baseline and ratio < minimum_ratio:
            flags.append("LOW_RAW_COVERAGE")
        if missing_indicators:
            flags.append("INDICATOR_GAP")

        rows.append(
            {
                "date": session_date,
                "raw_count": len(raw_tickers),
                "baseline_count": baseline,
                "coverage_ratio": ratio,
                "missing_from_previous": len(missing_previous),
                "missing_from_baseline": len(missing_baseline),
                "new_vs_previous": len(new_vs_previous),
                "indicator_count": len(indicator_tickers),
                "missing_indicators": len(missing_indicators),
                "flags": ",".join(flags) or "OK",
                "missing_tickers": sorted(missing_previous),
                "missing_baseline_tickers": sorted(missing_baseline),
                "missing_indicator_tickers": sorted(missing_indicators),
            }
        )

        if raw_tickers:
            populated_history.append((len(raw_tickers), raw_tickers))
            previous_tickers = raw_tickers

    return rows


def _ticker_lines(tickers: list[str], width: int = 20) -> list[str]:
    if not tickers:
        return ["none"]
    return [", ".join(tickers[index:index + width]) for index in range(0, len(tickers), width)]


def render_log(
    rows: list[dict],
    *,
    start_date,
    end_date,
    neon_missing_by_date: dict | None = None,
    neon_only_by_date: dict | None = None,
    neon_missing_indicators_by_date: dict | None = None,
    neon_only_indicators_by_date: dict | None = None,
) -> str:
    neon_missing_by_date = neon_missing_by_date or {}
    neon_only_by_date = neon_only_by_date or {}
    neon_missing_indicators_by_date = neon_missing_indicators_by_date or {}
    neon_only_indicators_by_date = neon_only_indicators_by_date or {}
    missing_sessions = [row for row in rows if "MISSING_SESSION" in row["flags"]]
    low_coverage = [row for row in rows if "LOW_RAW_COVERAGE" in row["flags"]]
    indicator_gaps = [row for row in rows if row["missing_indicators"]]
    lines = [
        "EQUITY SESSION COVERAGE AUDIT",
        f"Range: {start_date} -> {end_date}",
        f"Required raw coverage: {MIN_SESSION_COVERAGE_RATIO:.0%} of maximum prior 5 populated sessions",
        "",
        "SUMMARY",
        f"Expected NYSE sessions: {len(rows)}",
        f"Missing full sessions: {len(missing_sessions)}",
        f"Low raw-coverage sessions: {len(low_coverage)}",
        f"Sessions with indicator gaps: {len(indicator_gaps)}",
        f"Local rows missing from Neon: {sum(len(value) for value in neon_missing_by_date.values()):,}",
        f"Neon-only rows: {sum(len(value) for value in neon_only_by_date.values()):,}",
        "Local indicator rows missing from Neon: "
        f"{sum(len(value) for value in neon_missing_indicators_by_date.values()):,}",
        "Neon-only indicator rows: "
        f"{sum(len(value) for value in neon_only_indicators_by_date.values()):,}",
        "",
        "SESSION TABLE",
        "date       raw    baseline coverage base_missing prev_missing new indicator ind_missing flags",
    ]
    for row in rows:
        coverage = f"{row['coverage_ratio']:.2%}" if row["baseline_count"] else "n/a"
        lines.append(
            f"{row['date']} "
            f"{row['raw_count']:>6,} "
            f"{row['baseline_count']:>8,} "
            f"{coverage:>8} "
            f"{row['missing_from_baseline']:>12,} "
            f"{row['missing_from_previous']:>12,} "
            f"{row['new_vs_previous']:>3,} "
            f"{row['indicator_count']:>9,} "
            f"{row['missing_indicators']:>11,} "
            f"{row['flags']}"
        )

    lines.extend(["", "ANOMALY DETAILS"])
    anomalies = [
        row
        for row in rows
        if row["flags"] != "OK"
        or row["date"] in neon_missing_by_date
        or row["date"] in neon_only_by_date
        or row["date"] in neon_missing_indicators_by_date
        or row["date"] in neon_only_indicators_by_date
    ]
    if not anomalies:
        lines.append("No anomalies found.")
    for row in anomalies:
        lines.extend(
            [
                "",
                f"[{row['date']}] {row['flags']}",
                f"raw={row['raw_count']:,} baseline={row['baseline_count']:,} "
                f"coverage={row['coverage_ratio']:.2%}",
                f"missing_from_baseline={row['missing_from_baseline']:,} "
                f"missing_from_previous={row['missing_from_previous']:,} "
                f"new_vs_previous={row['new_vs_previous']:,}",
                "Missing baseline-session tickers:",
            ]
        )
        lines.extend(
            f"  {line}" for line in _ticker_lines(row["missing_baseline_tickers"])
        )
        lines.extend(
            [
                "Missing prior-session tickers:",
            ]
        )
        lines.extend(f"  {line}" for line in _ticker_lines(row["missing_tickers"]))
        if row["missing_indicators"]:
            lines.append("Missing same-date indicator tickers:")
            lines.extend(
                f"  {line}" for line in _ticker_lines(row["missing_indicator_tickers"])
            )
        if row["date"] in neon_missing_by_date:
            lines.append("Local ticker/date keys missing from Neon:")
            lines.extend(
                f"  {line}" for line in _ticker_lines(sorted(neon_missing_by_date[row["date"]]))
            )
        if row["date"] in neon_only_by_date:
            lines.append("Neon-only ticker/date keys:")
            lines.extend(
                f"  {line}" for line in _ticker_lines(sorted(neon_only_by_date[row["date"]]))
            )
        if row["date"] in neon_missing_indicators_by_date:
            lines.append("Local indicator keys missing from Neon:")
            lines.extend(
                f"  {line}"
                for line in _ticker_lines(sorted(neon_missing_indicators_by_date[row["date"]]))
            )
        if row["date"] in neon_only_indicators_by_date:
            lines.append("Neon-only indicator keys:")
            lines.extend(
                f"  {line}"
                for line in _ticker_lines(sorted(neon_only_indicators_by_date[row["date"]]))
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    start_date = pd.Timestamp(args.start_date).date()
    end_date = (
        pd.Timestamp(args.end_date).date()
        if args.end_date
        else latest_completed_nyse_session_date()
    )
    history_start = start_date - timedelta(days=14)
    sessions_with_history = expected_sessions(history_start, end_date)
    report_sessions = [date for date in sessions_with_history if date >= start_date]

    local = local_engine()
    core_only = args.coverage_universe == "core"
    eligible_tickers = fetch_coverage_eligible_tickers(local) if core_only else None
    local_raw = fetch_raw_coverage_keys(
        local,
        history_start,
        end_date,
        core_only=core_only,
        eligible_tickers=eligible_tickers,
    )
    local_indicators = fetch_keys(local, INDICATOR_TABLE, history_start, end_date)
    rows_with_history = build_session_rows(
        sessions_with_history,
        keys_by_date(local_raw),
        keys_by_date(local_indicators),
    )
    rows = [row for row in rows_with_history if row["date"] >= start_date]

    neon_missing_by_date: dict = {}
    neon_only_by_date: dict = {}
    neon_missing_indicators_by_date: dict = {}
    neon_only_indicators_by_date: dict = {}
    if args.include_neon:
        neon = neon_engine()
        neon_raw = keys_by_date(
            fetch_raw_coverage_keys(
                neon,
                start_date,
                end_date,
                core_only=core_only,
                eligible_tickers=eligible_tickers,
            )
        )
        neon_indicators = keys_by_date(fetch_keys(neon, INDICATOR_TABLE, start_date, end_date))
        local_raw_by_date = keys_by_date(
            local_raw[pd.to_datetime(local_raw["date"]).dt.date >= start_date]
        )
        local_indicators_by_date = keys_by_date(
            local_indicators[pd.to_datetime(local_indicators["date"]).dt.date >= start_date]
        )
        for session_date in report_sessions:
            local_keys = local_raw_by_date.get(session_date, set())
            neon_keys = neon_raw.get(session_date, set())
            if local_keys - neon_keys:
                neon_missing_by_date[session_date] = local_keys - neon_keys
            if neon_keys - local_keys:
                neon_only_by_date[session_date] = neon_keys - local_keys
            local_indicator_keys = local_indicators_by_date.get(session_date, set())
            neon_indicator_keys = neon_indicators.get(session_date, set())
            if local_indicator_keys - neon_indicator_keys:
                neon_missing_indicators_by_date[session_date] = (
                    local_indicator_keys - neon_indicator_keys
                )
            if neon_indicator_keys - local_indicator_keys:
                neon_only_indicators_by_date[session_date] = (
                    neon_indicator_keys - local_indicator_keys
                )

    output = args.output or (
        _bootstrap.LOGS_PATH
        / f"equity_coverage_audit_{start_date}_{end_date}_{pd.Timestamp.now():%Y%m%d_%H%M%S}.log"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    content = render_log(
        rows,
        start_date=start_date,
        end_date=end_date,
        neon_missing_by_date=neon_missing_by_date,
        neon_only_by_date=neon_only_by_date,
        neon_missing_indicators_by_date=neon_missing_indicators_by_date,
        neon_only_indicators_by_date=neon_only_indicators_by_date,
    )
    output.write_text(content, encoding="utf-8")
    print(content.split("ANOMALY DETAILS", 1)[0], flush=True)
    print(f"[AUDIT LOG] {output}", flush=True)


if __name__ == "__main__":
    main()
