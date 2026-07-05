from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.abs_metadata import (
    DEFAULT_ABS_DATAFLOWS,
    build_abs_series_mappings,
    decode_abs_series_id,
    fetch_and_store_abs_metadata,
    upsert_abs_series_mappings,
)
from db_builder.config import local_engine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch ABS SDMX metadata and decode ABS series IDs.")
    parser.add_argument("--dataflows", default=",".join(DEFAULT_ABS_DATAFLOWS))
    parser.add_argument("--skip-fetch", action="store_true", help="Use already stored ABS metadata without refetching.")
    parser.add_argument("--decode", default="", help="Optional ABS series ID to decode after metadata fetch.")
    parser.add_argument("--map-all", action="store_true", help="Decode all ABS economic series into abs_series_mappings.")
    parser.add_argument(
        "--update-series-names",
        action="store_true",
        help="Also update public.economic_indicators.series_name for ABS rows.",
    )
    parser.add_argument("--preview", type=int, default=10, help="Number of decoded mappings to print.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    dataflows = [item.strip() for item in args.dataflows.split(",") if item.strip()]
    if args.skip_fetch:
        print("[ABS METADATA] skipped fetch; using stored metadata")
    else:
        results = fetch_and_store_abs_metadata(engine, dataflows=dataflows)
        for dataflow, result in results.items():
            print(f"[ABS METADATA] {dataflow} dimensions={result['dimensions']:,} codes={result['codes']:,}")
    if args.decode:
        decoded = decode_abs_series_id(engine, args.decode)
        print(decoded)
    if args.map_all:
        mappings = build_abs_series_mappings(engine)
        mapped_count = upsert_abs_series_mappings(
            engine,
            mappings,
            update_series_names=args.update_series_names,
        )
        print(f"[ABS MAPPINGS] decoded_series={mapped_count:,}")
        if args.update_series_names:
            print("[ABS MAPPINGS] economic_indicators.series_name updated for ABS rows")
        for item in mappings[: max(args.preview, 0)]:
            print(f"{item['series_id']} -> {item['decoded_name']}")


if __name__ == "__main__":
    main()
