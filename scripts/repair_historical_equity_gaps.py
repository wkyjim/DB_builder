from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import INDICATOR_TABLE, RAW_TABLE, local_engine, neon_engine
from db_builder.eastmoney import _load_target_table, _upsert_dataframe
from db_builder.indicators import INDICATOR_COLUMNS, calculate_group
from db_builder.neon_sync import clean_dataframe_for_target
from db_builder.trading_calendar import (
    MIN_SESSION_COVERAGE_RATIO,
    latest_completed_nyse_session_date,
)
from db_builder.yfinance_equity_fallback import fetch_missing_range_rows
from equity_session_coverage_audit import (
    build_session_rows,
    expected_sessions,
    fetch_keys,
    keys_by_date,
)


ARTIFACT_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "equity_gap_repair"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair partial historical equity sessions with yfinance and rebuild indicators."
    )
    parser.add_argument("--start-date", default="2026-05-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--minimum-coverage", type=float, default=MIN_SESSION_COVERAGE_RATIO)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument(
        "--repair-csv",
        type=Path,
        default=None,
        help="Reuse a reviewed raw-repair CSV from a prior dry run instead of refetching.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply raw and indicator repairs to local PostgreSQL and Neon.",
    )
    return parser.parse_args()


def discover_repair_targets(engine, start_date, end_date, minimum_coverage: float) -> dict:
    history_start = start_date - timedelta(days=14)
    sessions = expected_sessions(history_start, end_date)
    raw = fetch_keys(engine, RAW_TABLE, history_start, end_date)
    rows = build_session_rows(
        sessions,
        keys_by_date(raw),
        defaultdict(set),
        minimum_ratio=minimum_coverage,
    )
    return {
        row["date"]: set(row["missing_baseline_tickers"])
        for row in rows
        if row["date"] >= start_date
        and "LOW_RAW_COVERAGE" in row["flags"]
        and row["missing_baseline_tickers"]
    }


def fetch_universe(engine, target_date, tickers: set[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    return pd.read_sql(
        text(
            f"""
            WITH target_tickers AS (
                SELECT unnest(:tickers) AS ticker
            )
            SELECT prior.ticker, prior.name, prior.market, prior.close,
                   prior.mkt_cap, prior.pe_ttm
            FROM target_tickers target
            CROSS JOIN LATERAL (
                SELECT ticker, name, market, close, mkt_cap, pe_ttm
                FROM {RAW_TABLE}
                WHERE ticker = target.ticker
                  AND date < :target_date
                ORDER BY date DESC
                LIMIT 1
            ) prior
            ORDER BY prior.ticker
            """
        ),
        engine,
        params={"tickers": sorted(tickers), "target_date": target_date},
    )


def fetch_repair_rows(
    engine,
    targets: dict,
    *,
    batch_size: int,
) -> tuple[pd.DataFrame, dict]:
    all_tickers = set().union(*targets.values()) if targets else set()
    first_target = min(targets) if targets else None
    universe = fetch_universe(engine, first_target, all_tickers)
    result = fetch_missing_range_rows(
        universe,
        targets,
        batch_size=batch_size,
    )
    diagnostics: dict = {}
    for target_date, tickers in sorted(targets.items()):
        found = {
            ticker
            for ticker, found_date in result.found_keys
            if found_date == target_date
        }
        unresolved = sorted(tickers - found)
        diagnostics[target_date.isoformat()] = {
            "requested": len(tickers),
            "found": len(found),
            "unresolved": len(unresolved),
            "unresolved_tickers": unresolved,
        }
        print(
            f"[YF DATE RESULT] date={target_date} requested={len(tickers):,} "
            f"found={len(found):,} unresolved={len(unresolved):,}",
            flush=True,
        )
    return result.frame.sort_values(["ticker", "date"]), diagnostics


def earliest_repair_dates(raw_repairs: pd.DataFrame) -> dict[str, object]:
    return (
        raw_repairs.groupby("ticker")["date"]
        .min()
        .to_dict()
    )


def calculate_replacement_indicators(engine, repair_dates: dict[str, object]) -> pd.DataFrame:
    if not repair_dates:
        return pd.DataFrame(columns=["date", "ticker"] + INDICATOR_COLUMNS)
    raw = pd.read_sql(
        text(
            f"""
            SELECT date, ticker, open, high, low, close, volume
            FROM {RAW_TABLE}
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date
            """
        ),
        engine,
        params={"tickers": sorted(repair_dates)},
    )
    results: list[pd.DataFrame] = []
    for ticker, group in raw.groupby("ticker", sort=False):
        calculated = calculate_group(group)
        first_date = pd.Timestamp(repair_dates[str(ticker)]).date()
        replacement = calculated[
            pd.to_datetime(calculated["date"]).dt.date >= first_date
        ].copy()
        if not replacement.empty:
            results.append(replacement)
    if not results:
        return pd.DataFrame(columns=["date", "ticker"] + INDICATOR_COLUMNS)
    frame = pd.concat(results, ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame[["date", "ticker"] + INDICATOR_COLUMNS]


def atomic_replace_indicators(engine, frame: pd.DataFrame, repair_dates: dict[str, object]) -> int:
    if frame.empty:
        return 0
    cleaned = clean_dataframe_for_target(frame, INDICATOR_TABLE)
    temp_rows = pd.DataFrame(
        [
            {"ticker": ticker, "start_date": pd.Timestamp(start_date).date()}
            for ticker, start_date in repair_dates.items()
        ]
    )
    columns = cleaned.columns.tolist()
    updates = [column for column in columns if column not in {"ticker", "date"}]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TEMP TABLE repair_indicator_ranges (
                    ticker text PRIMARY KEY,
                    start_date date NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        temp_rows.to_sql(
            "repair_indicator_ranges",
            connection,
            if_exists="append",
            index=False,
            method="multi",
        )
        connection.execute(
            text(
                f"""
                DELETE FROM {INDICATOR_TABLE} target
                USING repair_indicator_ranges repair
                WHERE target.ticker = repair.ticker
                  AND target.date >= repair.start_date
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE TEMP TABLE repair_indicator_rows
                (LIKE {INDICATOR_TABLE} INCLUDING DEFAULTS)
                ON COMMIT DROP
                """
            )
        )
        cleaned.to_sql(
            "repair_indicator_rows",
            connection,
            if_exists="append",
            index=False,
            chunksize=2000,
            method="multi",
        )
        connection.execute(
            text(
                f"""
                INSERT INTO {INDICATOR_TABLE} ({", ".join(columns)})
                SELECT {", ".join(columns)}
                FROM repair_indicator_rows
                ON CONFLICT (ticker, date)
                DO UPDATE SET {", ".join(f"{column} = EXCLUDED.{column}" for column in updates)}
                """
            )
        )
    return len(cleaned)


def count_indicator_scope(engine, repair_dates: dict[str, object]) -> int:
    if not repair_dates:
        return 0
    total = 0
    with engine.connect() as connection:
        for ticker, start_date in repair_dates.items():
            total += connection.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM {INDICATOR_TABLE}
                    WHERE ticker = :ticker AND date >= :start_date
                    """
                ),
                {"ticker": ticker, "start_date": start_date},
            ).scalar_one()
    return total


def write_diagnostics(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> None:
    args = parse_args()
    start_date = pd.Timestamp(args.start_date).date()
    end_date = (
        pd.Timestamp(args.end_date).date()
        if args.end_date
        else latest_completed_nyse_session_date()
    )
    local = local_engine(use_insertmanyvalues=True)
    targets = discover_repair_targets(
        local,
        start_date,
        end_date,
        args.minimum_coverage,
    )
    print(
        f"[REPAIR PLAN] dates={len(targets):,} "
        f"candidate_keys={sum(len(value) for value in targets.values()):,}",
        flush=True,
    )
    for target_date, tickers in sorted(targets.items()):
        print(f"  {target_date}: {len(tickers):,} candidate missing tickers", flush=True)

    run_id = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    artifact_dir = args.artifact_dir / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    raw_path = artifact_dir / "raw_repairs.csv"
    diagnostics_path = artifact_dir / "diagnostics.json"
    if args.repair_csv:
        raw_repairs = pd.read_csv(
            args.repair_csv,
            parse_dates=["date"],
            keep_default_na=False,
            na_values=[""],
        )
        raw_repairs["date"] = raw_repairs["date"].dt.date
        diagnostics = {
            "source": str(args.repair_csv.resolve()),
            "mode": "reviewed_csv_reuse",
        }
        print(f"[REPAIR CSV REUSE] source={args.repair_csv}", flush=True)
    else:
        raw_repairs, diagnostics = fetch_repair_rows(
            local,
            targets,
            batch_size=args.batch_size,
        )
    raw_repairs.to_csv(raw_path, index=False)
    write_diagnostics(diagnostics_path, diagnostics)
    print(f"[RAW REPAIR CSV] path={raw_path} rows={len(raw_repairs):,}", flush=True)
    print(f"[DIAGNOSTICS] path={diagnostics_path}", flush=True)

    if raw_repairs.empty:
        print("[NO REPAIRS] yfinance returned no missing session rows.", flush=True)
        return

    repair_dates = earliest_repair_dates(raw_repairs)
    print(
        f"[IMPACTED TICKERS] count={len(repair_dates):,} "
        f"earliest={min(repair_dates.values())}",
        flush=True,
    )
    local_delete_count = count_indicator_scope(local, repair_dates)
    print(
        f"[LOCAL INDICATOR REPLACEMENT SCOPE] existing_rows={local_delete_count:,}",
        flush=True,
    )
    if not args.apply:
        print("[DRY RUN] No database rows changed. Re-run with --apply.", flush=True)
        return

    local_raw_table = _load_target_table(local, RAW_TABLE)
    _upsert_dataframe(local, local_raw_table, raw_repairs)
    print(f"[LOCAL RAW UPSERT] rows={len(raw_repairs):,}", flush=True)

    replacements = calculate_replacement_indicators(local, repair_dates)
    indicator_path = artifact_dir / "indicator_replacements.csv"
    replacements.to_csv(indicator_path, index=False)
    print(
        f"[INDICATOR REPLACEMENT CSV] path={indicator_path} rows={len(replacements):,}",
        flush=True,
    )
    print(
        f"[LOCAL INDICATOR REPLACE] rows={atomic_replace_indicators(local, replacements, repair_dates):,}",
        flush=True,
    )

    neon = neon_engine(use_insertmanyvalues=True)
    neon_raw_table = _load_target_table(neon, RAW_TABLE)
    _upsert_dataframe(neon, neon_raw_table, raw_repairs)
    print(f"[NEON RAW UPSERT] rows={len(raw_repairs):,}", flush=True)
    print(
        f"[NEON INDICATOR REPLACE] rows={atomic_replace_indicators(neon, replacements, repair_dates):,}",
        flush=True,
    )
    print("[REPAIR COMPLETE]", flush=True)


if __name__ == "__main__":
    main()
