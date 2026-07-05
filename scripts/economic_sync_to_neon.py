from __future__ import annotations

import argparse

import pandas as pd
from psycopg2.extras import Json, execute_values
from sqlalchemy import text

import _bootstrap  # noqa: F401

from db_builder.config import local_engine, neon_engine
from db_builder.economic_data import _json_safe, create_economic_indicators_table


COLUMNS = [
    "date",
    "series_id",
    "series_name",
    "source",
    "country",
    "region",
    "category",
    "frequency",
    "value",
    "unit",
    "seasonal_adjustment",
    "realtime_start",
    "realtime_end",
    "updated_at",
    "raw_payload",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync local economic indicators to Neon.")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=1000)
    return parser.parse_args()


def fetch_local_rows(engine, *, start_date: str) -> pd.DataFrame:
    return pd.read_sql(
        text(
            """
            SELECT date, series_id, series_name, source, country, region, category,
                   frequency, value, unit, seasonal_adjustment, realtime_start,
                   realtime_end, updated_at, raw_payload
            FROM public.economic_indicators
            WHERE date >= DATE :start_date
            ORDER BY source, category, series_id, date
            """
        ),
        engine,
        params={"start_date": start_date},
    )


def bulk_upsert_neon(engine, rows: list[dict], *, chunk_size: int = 1000) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO public.economic_indicators (
            date, series_id, series_name, source, country, region, category,
            frequency, value, unit, seasonal_adjustment, realtime_start,
            realtime_end, updated_at, raw_payload
        ) VALUES %s
        ON CONFLICT (series_id, date, realtime_start)
        DO UPDATE SET
            series_name = EXCLUDED.series_name,
            source = EXCLUDED.source,
            country = EXCLUDED.country,
            region = EXCLUDED.region,
            category = EXCLUDED.category,
            frequency = EXCLUDED.frequency,
            value = EXCLUDED.value,
            unit = EXCLUDED.unit,
            seasonal_adjustment = EXCLUDED.seasonal_adjustment,
            realtime_end = EXCLUDED.realtime_end,
            updated_at = now(),
            raw_payload = EXCLUDED.raw_payload
    """
    records = [
        tuple(Json(_json_safe(row[col])) if col == "raw_payload" else row[col] for col in COLUMNS)
        for row in rows
    ]
    conn = engine.raw_connection()
    try:
        cursor = conn.cursor()
        total = 0
        for start in range(0, len(records), chunk_size):
            chunk = records[start:start + chunk_size]
            execute_values(cursor, sql, chunk, page_size=len(chunk))
            conn.commit()
            total += len(chunk)
            print(f"[NEON ECON UPSERT] {total:,}/{len(records):,}", flush=True)
    finally:
        conn.close()
    return len(records)


def print_summary(engine, *, start_date: str) -> None:
    summary = pd.read_sql(
        text(
            """
            SELECT source, category, COUNT(*) AS rows, COUNT(DISTINCT series_id) AS series_count,
                   MIN(date) AS min_date, MAX(date) AS max_date
            FROM public.economic_indicators
            WHERE date >= DATE :start_date
            GROUP BY source, category
            ORDER BY source, category
            """
        ),
        engine,
        params={"start_date": start_date},
    )
    print(summary.to_string(index=False) if not summary.empty else "No Neon economic rows found.", flush=True)


def main() -> None:
    args = parse_args()
    local = local_engine(use_insertmanyvalues=True)
    neon = neon_engine(use_insertmanyvalues=True)
    create_economic_indicators_table(neon)

    df = fetch_local_rows(local, start_date=args.start_date)
    print(f"[LOCAL ECON ROWS] rows={len(df):,} start_date={args.start_date}", flush=True)
    if args.dry_run:
        print("[DRY RUN] no Neon writes", flush=True)
        return

    count = bulk_upsert_neon(neon, df.to_dict(orient="records"), chunk_size=args.chunk_size)
    print(f"[NEON ECON SYNC DONE] rows={count:,}", flush=True)
    print_summary(neon, start_date=args.start_date)


if __name__ == "__main__":
    main()
