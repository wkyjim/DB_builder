from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
import pandas as pd

from db_builder.config import local_engine
from db_builder.economic_data import create_economic_indicators_table, upsert_economic_indicators
from db_builder.global_economic_data import fetch_global_economic_rows, rows_to_json_preview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch global economic data from free World Bank, ECB, and ABS endpoints.")
    parser.add_argument("--start-date", default="2026-04-01")
    parser.add_argument("--providers", default="world_bank,ecb,abs", help="Comma-separated: world_bank,ecb,abs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--preview", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    providers = {item.strip() for item in args.providers.split(",") if item.strip()}
    engine = local_engine(use_insertmanyvalues=True)
    create_economic_indicators_table(engine)

    fetched = fetch_global_economic_rows(start_date=args.start_date, providers=providers)
    all_rows = []
    for provider, rows in fetched.items():
        all_rows.extend(rows)
        latest = max((row["date"] for row in rows), default="n/a")
        print(f"[FETCHED] {provider} rows={len(rows):,} latest={latest}")
        if args.preview:
            print(rows_to_json_preview(rows, limit=3))

    if args.dry_run:
        print(f"[DRY RUN] fetched_rows={len(all_rows):,}; no database writes")
        return

    if not args.upsert_local:
        print("[NO WRITE] pass --upsert-local to write fetched rows")
        return

    total = upsert_economic_indicators(engine, all_rows)
    print(f"[UPSERT LOCAL] rows={total:,}")
    summary = (
        pd.DataFrame(all_rows)
        .groupby(["source", "category"], dropna=False)
        .agg(rows=("series_id", "size"), series_count=("series_id", "nunique"), min_date=("date", "min"), max_date=("date", "max"))
        .reset_index()
        .sort_values(["source", "category"])
    )
    print(summary.to_string(index=False) if not summary.empty else "No rows fetched.")


if __name__ == "__main__":
    main()
