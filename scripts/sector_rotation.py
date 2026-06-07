from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.sector_rotation import generate_sector_rotation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate sector rotation signals.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--dry-run", action="store_true", help="Print sector rotation only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert sector rotation into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine(use_insertmanyvalues=True)
    signals = generate_sector_rotation(engine, window_hours=args.window_hours, dry_run=dry_run)

    print(f"Window {args.window_hours}h: generated {len(signals):,} sector rotation signal(s)")
    if dry_run:
        print("[dry-run] sector rotation was not written")
    for signal in signals[:20]:
        print(
            f"- rank={signal['rotation_rank']} sector={signal['sector_name']} "
            f"score={signal['rotation_score']} bias={signal['allocation_bias']} "
            f"action={signal['recommended_action']} etfs={','.join(signal['related_etfs'])}"
        )


if __name__ == "__main__":
    main()
