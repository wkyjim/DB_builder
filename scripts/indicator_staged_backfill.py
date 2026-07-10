from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import INDICATOR_TABLE, RAW_TABLE, local_engine, neon_engine
from db_builder.indicators import (
    INDICATOR_COLUMNS,
    LOOKBACK_ROWS,
    bulk_upsert_with_progress,
    calculate_group,
)
from db_builder.neon_sync import bulk_temp_staging_upsert_to_neon, update_sync_state
from db_builder.trading_calendar import is_valid_nyse_session


ARTIFACT_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "indicator_backfill"
CSV_READ_OPTIONS = {"keep_default_na": False, "na_values": [""]}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resumable CSV-staged indicator backfill.")
    parser.add_argument(
        "--date",
        help="Target NYSE session date in YYYY-MM-DD. Defaults to latest local raw date.",
    )
    parser.add_argument(
        "--stage",
        choices=["export", "calculate", "upsert-local", "upsert-neon", "all"],
        default="all",
    )
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument("--chunk-rows", type=int, default=100_000)
    parser.add_argument("--force-export", action="store_true")
    parser.add_argument("--force-calculate", action="store_true")
    parser.add_argument(
        "--cleanup-on-success",
        action="store_true",
        help="Delete target-date CSV artifacts only after all requested stages succeed.",
    )
    return parser.parse_args()


def latest_raw_date(engine) -> str:
    with engine.connect() as connection:
        value = connection.execute(text(f"SELECT MAX(date) FROM {RAW_TABLE}")).scalar()
    if value is None:
        raise RuntimeError("No local raw equity date is available.")
    return pd.Timestamp(value).date().isoformat()


def missing_local_tickers(engine, target_date: str) -> list[str]:
    rows = pd.read_sql(
        text(
            f"""
            SELECT r.ticker
            FROM {RAW_TABLE} r
            LEFT JOIN {INDICATOR_TABLE} i
              ON i.ticker = r.ticker
             AND i.date = r.date
            WHERE r.date = DATE :target_date
              AND i.ticker IS NULL
            ORDER BY r.ticker
            """
        ),
        engine,
        params={"target_date": target_date},
    )
    return rows["ticker"].astype(str).tolist()


def export_raw_snapshot(engine, tickers: list[str], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not tickers:
        pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume"]).to_csv(
            path,
            index=False,
        )
        return 0

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE target_indicator_tickers (ticker text PRIMARY KEY) ON COMMIT DROP")
        execute_values(
            cursor,
            "INSERT INTO target_indicator_tickers (ticker) VALUES %s",
            [(ticker,) for ticker in tickers],
            page_size=1000,
        )
        copy_sql = f"""
            COPY (
                SELECT r.date, r.ticker, r.open, r.high, r.low, r.close, r.volume
                FROM {RAW_TABLE} r
                JOIN target_indicator_tickers t ON t.ticker = r.ticker
                ORDER BY r.ticker, r.date
            ) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)
        """
        with path.open("w", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(copy_sql, handle)
        connection.commit()
    finally:
        connection.close()

    with path.open("r", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def csv_tickers(path: Path, *, chunk_rows: int = 100_000) -> set[str]:
    if not path.exists() or not path.stat().st_size:
        return set()
    tickers: set[str] = set()
    for chunk in pd.read_csv(
        path,
        usecols=["ticker"],
        chunksize=chunk_rows,
        **CSV_READ_OPTIONS,
    ):
        tickers.update(chunk["ticker"].astype(str))
    return tickers


def _calculate_groups(
    frame: pd.DataFrame,
    *,
    target_date: pd.Timestamp,
    processed: set[str],
) -> list[pd.DataFrame]:
    results: list[pd.DataFrame] = []
    for ticker, group in frame.groupby("ticker", sort=False):
        ticker = str(ticker)
        if ticker in processed:
            continue
        calculated = calculate_group(group.tail(LOOKBACK_ROWS))
        target = calculated[pd.to_datetime(calculated["date"]) == target_date].copy()
        if not target.empty:
            results.append(target)
    return results


def calculate_indicator_csv(
    raw_path: Path,
    output_path: Path,
    *,
    target_date: str,
    chunk_rows: int,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    processed: set[str] = set()
    if output_path.exists() and output_path.stat().st_size:
        processed = set(
            pd.read_csv(
                output_path,
                usecols=["ticker"],
                **CSV_READ_OPTIONS,
            )["ticker"].astype(str)
        )

    target_timestamp = pd.Timestamp(target_date)
    carry = pd.DataFrame()
    written = len(processed)

    for chunk_number, chunk in enumerate(
        pd.read_csv(
            raw_path,
            chunksize=chunk_rows,
            parse_dates=["date"],
            **CSV_READ_OPTIONS,
        ),
        start=1,
    ):
        frame = pd.concat([carry, chunk], ignore_index=True)
        last_ticker = str(frame["ticker"].iloc[-1])
        carry = frame[frame["ticker"].astype(str) == last_ticker].copy()
        complete = frame[frame["ticker"].astype(str) != last_ticker]
        results = _calculate_groups(complete, target_date=target_timestamp, processed=processed)
        if results:
            batch = pd.concat(results, ignore_index=True)
            batch.to_csv(output_path, mode="a", header=not output_path.exists(), index=False)
            processed.update(batch["ticker"].astype(str))
            written += len(batch)
        print(
            f"[CALCULATE] chunk={chunk_number} processed_tickers={len(processed):,} "
            f"output_rows={written:,}",
            flush=True,
        )

    if not carry.empty:
        results = _calculate_groups(carry, target_date=target_timestamp, processed=processed)
        if results:
            batch = pd.concat(results, ignore_index=True)
            batch.to_csv(output_path, mode="a", header=not output_path.exists(), index=False)
            written += len(batch)

    return written


def load_and_validate_indicator_csv(path: Path, target_date: str) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["date"], **CSV_READ_OPTIONS)
    frame["date"] = frame["date"].dt.date
    expected_date = pd.Timestamp(target_date).date()
    if frame.empty:
        raise ValueError("Indicator CSV is empty.")
    if set(frame["date"]) != {expected_date}:
        raise ValueError("Indicator CSV contains dates outside the requested target date.")
    if frame.duplicated(["ticker", "date"]).any():
        raise ValueError("Indicator CSV contains duplicate ticker/date keys.")
    if not is_valid_nyse_session(expected_date):
        raise ValueError(f"{target_date} is not a valid NYSE session.")
    return frame[["date", "ticker"] + INDICATOR_COLUMNS]


def upsert_local_csv(engine, frame: pd.DataFrame) -> int:
    bulk_upsert_with_progress(engine, frame, indicator_table=INDICATOR_TABLE)
    return len(frame)


def fetch_local_target_indicators(engine, target_date: str) -> pd.DataFrame:
    return pd.read_sql(
        text(
            f"""
            SELECT date, ticker, {", ".join(INDICATOR_COLUMNS)}
            FROM {INDICATOR_TABLE}
            WHERE date = DATE :target_date
            ORDER BY ticker
            """
        ),
        engine,
        params={"target_date": target_date},
    )


def upsert_neon_csv(engine, frame: pd.DataFrame, target_date: str) -> int:
    existing = pd.read_sql(
        text(f"SELECT ticker FROM {INDICATOR_TABLE} WHERE date = DATE :target_date"),
        engine,
        params={"target_date": target_date},
    )
    existing_tickers = set(existing["ticker"].astype(str))
    missing = frame[~frame["ticker"].astype(str).isin(existing_tickers)].copy()
    print(f"[NEON MISSING] rows={len(missing):,}", flush=True)
    if missing.empty:
        return 0
    latest = bulk_temp_staging_upsert_to_neon(
        neon_engine=engine,
        df=missing,
        target_table=INDICATOR_TABLE,
        conflict_cols=["ticker", "date"],
        chunk_size=1000,
    )
    update_sync_state(engine, INDICATOR_TABLE, latest)
    return len(missing)


def main() -> None:
    args = parse_args()
    local = local_engine(use_insertmanyvalues=True)
    target_date = (
        pd.Timestamp(args.date).date().isoformat()
        if args.date
        else latest_raw_date(local)
    )
    print(f"[TARGET DATE] {target_date}", flush=True)
    raw_path = args.artifact_dir / f"raw_history_{target_date}.csv"
    indicator_path = args.artifact_dir / f"indicators_{target_date}.csv"

    stages = (
        ["export", "calculate", "upsert-local", "upsert-neon"]
        if args.stage == "all"
        else [args.stage]
    )
    missing_tickers = missing_local_tickers(local, target_date)
    needs_local_generation = bool(missing_tickers)
    print(
        f"[LOCAL MISSING TICKERS] date={target_date} count={len(missing_tickers):,}",
        flush=True,
    )

    if "export" in stages:
        if not needs_local_generation:
            print("[EXPORT SKIP] local target date is complete", flush=True)
        elif (
            args.force_export
            or not raw_path.exists()
            or not set(missing_tickers).issubset(csv_tickers(raw_path))
        ):
            rows = export_raw_snapshot(local, missing_tickers, raw_path)
            print(f"[RAW SNAPSHOT] path={raw_path} rows={rows:,}", flush=True)
        else:
            print(f"[RAW SNAPSHOT REUSE] {raw_path}", flush=True)

    if "calculate" in stages:
        if args.stage == "all" and not needs_local_generation:
            print("[CALCULATE SKIP] local target date is complete", flush=True)
        else:
            if args.force_calculate and indicator_path.exists():
                indicator_path.unlink()
            rows = calculate_indicator_csv(
                raw_path,
                indicator_path,
                target_date=target_date,
                chunk_rows=args.chunk_rows,
            )
            print(f"[INDICATOR CSV] path={indicator_path} rows={rows:,}", flush=True)

    frame = None
    if "upsert-local" in stages and (args.stage != "all" or needs_local_generation):
        frame = load_and_validate_indicator_csv(indicator_path, target_date)
        print(f"[VALIDATED CSV] rows={len(frame):,}", flush=True)

    if "upsert-local" in stages:
        if args.stage == "all" and not needs_local_generation:
            print("[LOCAL UPSERT SKIP] local target date is complete", flush=True)
        else:
            print(f"[LOCAL UPSERT DONE] rows={upsert_local_csv(local, frame):,}", flush=True)

    if "upsert-neon" in stages:
        remaining = missing_local_tickers(local, target_date)
        if remaining:
            raise RuntimeError(
                f"Local indicator coverage remains incomplete for {target_date}: "
                f"{len(remaining):,} tickers missing."
            )
        frame = fetch_local_target_indicators(local, target_date)
        print(f"[LOCAL TARGET SET] rows={len(frame):,}", flush=True)
        if frame.empty:
            raise RuntimeError(f"No local indicators are available for {target_date}.")
        neon = neon_engine(use_insertmanyvalues=True)
        print(f"[NEON UPSERT DONE] rows={upsert_neon_csv(neon, frame, target_date):,}", flush=True)

    if args.cleanup_on_success and args.stage == "all":
        for path in (raw_path, indicator_path):
            if path.exists():
                path.unlink()
                print(f"[CLEANUP] removed {path}", flush=True)


if __name__ == "__main__":
    main()
