"""Mirror public.macro_indicators from local PostgreSQL to Neon.

Neon is a bounded mirror: by default only rows on/after db_builder.config's
NEON_MIRROR_SINCE are kept. --since widens the window for a one-off backfill.
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import NEON_MIRROR_SINCE, local_engine, neon_engine
from db_builder.macro_indicators_neon import (
    count_macro_indicators,
    sync_macro_indicators_to_neon,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since", default=NEON_MIRROR_SINCE,
        help="Mirror rows on/after this date (YYYY-MM-DD) and prune older ones. "
             f"Default: {NEON_MIRROR_SINCE} (the served window).",
    )
    parser.add_argument("--local-only", action="store_true",
                        help="Report local row counts without touching Neon.")
    parser.add_argument("--chunk-size", type=int, default=500)
    args = parser.parse_args()

    local = local_engine()
    print(f"Local macro indicators: {count_macro_indicators(local):,} rows"
          f" (since {args.since})")
    if args.local_only:
        return 0

    result = sync_macro_indicators_to_neon(
        local, neon_engine(), since=args.since, chunk_size=args.chunk_size
    )
    print(f"Neon macro indicators: {result['remote_rows']:,} rows "
          f"(since {result['since']})")
    print(
        f"[MACRO INDICATORS NEON SYNC] uploaded={result['uploaded']:,} "
        f"pruned={result['pruned_rows']:,} remote_rows={result['remote_rows']:,} "
        f"failed={result['failed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
