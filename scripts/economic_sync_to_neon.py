from __future__ import annotations

import argparse

import pandas as pd
from sqlalchemy import text

import _bootstrap  # noqa: F401

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
              AND country = 'US'
            ORDER BY source, category, series_id, date
            """
        ),
        engine,
        params={"start_date": start_date},
    )


def main() -> None:
    parse_args()
    print(
        "[DISABLED] Economic indicators are local-only; Neon sync is permanently disabled.",
        flush=True,
    )


if __name__ == "__main__":
    main()
