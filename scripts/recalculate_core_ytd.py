from __future__ import annotations

import argparse
import math
from pathlib import Path

import pandas as pd
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import RAW_TABLE, local_engine, neon_engine


ARTIFACT_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "core_ytd"
CSV_COLUMNS = [
    "date",
    "ticker",
    "ytd_pct_chg",
    "base_date",
    "base_close",
    "base_method",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate core-universe YTD changes to CSV and update matching database rows."
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument(
        "--stage",
        choices=[
            "calculate",
            "update-local",
            "update-neon",
            "verify-local",
            "verify-neon",
            "all",
        ],
        default="calculate",
    )
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_ROOT)
    return parser.parse_args()


def default_csv_path(artifact_dir: Path, year: int) -> Path:
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    return artifact_dir / f"core_ytd_{year}_{stamp}.csv"


def export_calculated_csv(engine, path: Path, year: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    year_start = f"{year}-01-01"
    next_year = f"{year + 1}-01-01"
    copy_sql = f"""
        COPY (
            WITH eligible AS (
                SELECT ticker
                FROM public.equity_security_status
                WHERE coverage_eligible = true
            ),
            prior_anchor AS (
                SELECT DISTINCT ON (raw.ticker)
                    raw.ticker,
                    raw.date AS base_date,
                    raw.close AS base_close
                FROM {RAW_TABLE} raw
                JOIN eligible USING (ticker)
                WHERE raw.date < DATE '{year_start}'
                  AND raw.close IS NOT NULL
                  AND raw.close > 0
                ORDER BY raw.ticker, raw.date DESC
            ),
            first_current AS (
                SELECT DISTINCT ON (raw.ticker)
                    raw.ticker,
                    raw.date AS base_date,
                    raw.close AS base_close
                FROM {RAW_TABLE} raw
                JOIN eligible USING (ticker)
                WHERE raw.date >= DATE '{year_start}'
                  AND raw.date < DATE '{next_year}'
                  AND raw.close IS NOT NULL
                  AND raw.close > 0
                ORDER BY raw.ticker, raw.date
            ),
            anchors AS (
                SELECT
                    current.ticker,
                    COALESCE(prior.base_date, current.base_date) AS base_date,
                    COALESCE(prior.base_close, current.base_close) AS base_close,
                    CASE
                        WHEN prior.base_close IS NOT NULL THEN 'prior_year_close'
                        ELSE 'first_available_in_year'
                    END AS base_method
                FROM first_current current
                LEFT JOIN prior_anchor prior USING (ticker)
            ),
            daily_returns AS (
                SELECT
                    raw.date,
                    raw.ticker,
                    anchors.base_date,
                    anchors.base_close,
                    anchors.base_method,
                    CASE
                        WHEN anchors.base_method = 'first_available_in_year'
                         AND ROW_NUMBER() OVER (
                             PARTITION BY raw.ticker ORDER BY raw.date
                         ) = 1
                            THEN 0.0
                        WHEN raw.pct_chg IS NOT NULL
                         AND raw.pct_chg::double precision <> 'NaN'::double precision
                            THEN raw.pct_chg::double precision / 100.0
                        WHEN raw.prev_close IS NOT NULL
                         AND raw.prev_close::double precision <> 'NaN'::double precision
                         AND raw.prev_close > 0
                            THEN (raw.close::double precision / raw.prev_close::double precision) - 1.0
                        ELSE 0.0
                    END AS daily_return
                FROM {RAW_TABLE} raw
                JOIN anchors USING (ticker)
                WHERE raw.date >= DATE '{year_start}'
                  AND raw.date < DATE '{next_year}'
                  AND raw.close IS NOT NULL
                  AND raw.close > 0
            )
            SELECT
                date,
                ticker,
                (
                    EXP(
                        SUM(
                            LN(GREATEST(1.0 + daily_return, 0.000000000001))
                        ) OVER (
                            PARTITION BY ticker
                            ORDER BY date
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        )
                    ) - 1.0
                ) * 100.0 AS ytd_pct_chg,
                base_date,
                base_close,
                base_method
            FROM daily_returns
            ORDER BY date, ticker
        ) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)
    """
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        with path.open("w", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(copy_sql, handle)
        connection.commit()
    finally:
        connection.close()
    with path.open("r", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def validate_ytd_csv(path: Path, *, chunk_rows: int = 200_000) -> dict:
    if not path.exists() or not path.stat().st_size:
        raise ValueError(f"YTD CSV does not exist or is empty: {path}")
    seen: set[tuple[str, str]] = set()
    rows = 0
    tickers: set[str] = set()
    methods: dict[str, int] = {}
    minimum_date = None
    maximum_date = None
    maximum_absolute_ytd = 0.0
    for chunk in pd.read_csv(path, chunksize=chunk_rows, keep_default_na=False):
        if list(chunk.columns) != CSV_COLUMNS:
            raise ValueError(f"Unexpected CSV columns: {chunk.columns.tolist()}")
        chunk["date"] = pd.to_datetime(chunk["date"], errors="raise").dt.date
        chunk["ticker"] = chunk["ticker"].astype(str)
        values = pd.to_numeric(chunk["ytd_pct_chg"], errors="raise")
        bases = pd.to_numeric(chunk["base_close"], errors="raise")
        if not values.map(math.isfinite).all():
            raise ValueError("YTD CSV contains non-finite values.")
        if (bases <= 0).any():
            raise ValueError("YTD CSV contains a non-positive base close.")
        keys = list(zip(chunk["date"].astype(str), chunk["ticker"]))
        if len(keys) != len(set(keys)) or any(key in seen for key in keys):
            raise ValueError("YTD CSV contains duplicate ticker/date keys.")
        seen.update(keys)
        tickers.update(chunk["ticker"])
        rows += len(chunk)
        minimum_date = min(chunk["date"].min(), minimum_date) if minimum_date else chunk["date"].min()
        maximum_date = max(chunk["date"].max(), maximum_date) if maximum_date else chunk["date"].max()
        maximum_absolute_ytd = max(maximum_absolute_ytd, float(values.abs().max()))
        for method, count in chunk["base_method"].value_counts().items():
            methods[str(method)] = methods.get(str(method), 0) + int(count)
    if not rows:
        raise ValueError("YTD CSV contains no data rows.")
    return {
        "rows": rows,
        "tickers": len(tickers),
        "minimum_date": minimum_date,
        "maximum_date": maximum_date,
        "base_methods": methods,
        "maximum_absolute_ytd": maximum_absolute_ytd,
    }


def update_ytd_from_csv(engine, path: Path) -> int:
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TEMP TABLE core_ytd_update (
                date date NOT NULL,
                ticker text NOT NULL,
                ytd_pct_chg double precision NOT NULL,
                base_date date NOT NULL,
                base_close double precision NOT NULL,
                base_method text NOT NULL,
                PRIMARY KEY (date, ticker)
            ) ON COMMIT DROP
            """
        )
        with path.open("r", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(
                """
                COPY core_ytd_update (
                    date, ticker, ytd_pct_chg, base_date, base_close, base_method
                ) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)
                """,
                handle,
            )
        cursor.execute(
            f"""
            UPDATE {RAW_TABLE} target
            SET ytd_pct_chg = source.ytd_pct_chg
            FROM core_ytd_update source
            WHERE target.date = source.date
              AND target.ticker = source.ticker
              AND target.ytd_pct_chg IS DISTINCT FROM source.ytd_pct_chg
            """
        )
        updated = cursor.rowcount
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def verify_ytd_from_csv(engine, path: Path, tolerance: float = 1e-9) -> dict:
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TEMP TABLE core_ytd_verify (
                date date NOT NULL,
                ticker text NOT NULL,
                ytd_pct_chg double precision NOT NULL,
                base_date date NOT NULL,
                base_close double precision NOT NULL,
                base_method text NOT NULL,
                PRIMARY KEY (date, ticker)
            ) ON COMMIT DROP
            """
        )
        with path.open("r", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(
                """
                COPY core_ytd_verify (
                    date, ticker, ytd_pct_chg, base_date, base_close, base_method
                ) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)
                """,
                handle,
            )
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS csv_rows,
                COUNT(target.ticker) AS matched_rows,
                COUNT(*) FILTER (
                    WHERE target.ticker IS NOT NULL
                      AND (
                          target.ytd_pct_chg IS NULL
                          OR ABS(target.ytd_pct_chg - source.ytd_pct_chg) > %s
                      )
                ) AS mismatched_values
            FROM core_ytd_verify source
            LEFT JOIN {RAW_TABLE} target
              ON target.date = source.date
             AND target.ticker = source.ticker
            """,
            (tolerance,),
        )
        csv_rows, matched_rows, mismatched_values = cursor.fetchone()
        connection.rollback()
        return {
            "csv_rows": csv_rows,
            "matched_rows": matched_rows,
            "missing_rows": csv_rows - matched_rows,
            "mismatched_values": mismatched_values,
        }
    finally:
        connection.close()


def main() -> None:
    args = parse_args()
    csv_path = args.csv or default_csv_path(args.artifact_dir, args.year)
    stages = (
        ["calculate", "update-local", "update-neon"]
        if args.stage == "all"
        else [args.stage]
    )
    local = local_engine()
    if "calculate" in stages:
        rows = export_calculated_csv(local, csv_path, args.year)
        print(f"[CALCULATED CSV] path={csv_path} rows={rows:,}", flush=True)
    summary = validate_ytd_csv(csv_path)
    print(
        f"[CSV VALID] rows={summary['rows']:,} tickers={summary['tickers']:,} "
        f"range={summary['minimum_date']}->{summary['maximum_date']} "
        f"base_methods={summary['base_methods']} "
        f"max_abs_ytd={summary['maximum_absolute_ytd']:.2f}%",
        flush=True,
    )
    if "update-local" in stages:
        print(f"[LOCAL UPDATE] changed_rows={update_ytd_from_csv(local, csv_path):,}", flush=True)
    if "update-neon" in stages:
        print(
            f"[NEON UPDATE] changed_rows={update_ytd_from_csv(neon_engine(), csv_path):,}",
            flush=True,
        )
    if "verify-local" in stages:
        print(f"[LOCAL VERIFY] {verify_ytd_from_csv(local, csv_path)}", flush=True)
    if "verify-neon" in stages:
        print(
            f"[NEON VERIFY] {verify_ytd_from_csv(neon_engine(), csv_path)}",
            flush=True,
        )


if __name__ == "__main__":
    main()
