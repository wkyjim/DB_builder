"""Refresh the local latest short snapshot and sync changed rows to Neon."""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine, neon_engine
from db_builder.short_snapshot import refresh_local_latest_snapshot, sync_latest_snapshot_to_neon


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--skip-local-refresh", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=200)
    args = parser.parse_args()
    local = local_engine()
    if not args.skip_local_refresh:
        refreshed = refresh_local_latest_snapshot(local)
        print(f"Local latest short snapshot: {refreshed}")
    if not args.local_only:
        synced = sync_latest_snapshot_to_neon(local, neon_engine(), chunk_size=args.chunk_size)
        print(f"Neon latest short snapshot: {synced}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
