from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.sector_regime import generate_sector_regimes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate sector regime signals.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--dry-run", action="store_true", help="Print sector regimes only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert sector regimes into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine(use_insertmanyvalues=True)
    signals = generate_sector_regimes(engine, window_hours=args.window_hours, dry_run=dry_run)

    print(f"Window {args.window_hours}h: generated {len(signals):,} sector regime signal(s)")
    if dry_run:
        print("[dry-run] sector regimes were not written")
    for signal in signals[:20]:
        print(
            f"- sector={signal['sector_name']} regime={signal['sector_regime']} "
            f"phase={signal['cycle_phase']} confidence={signal['confidence']} "
            f"final={signal['final_score']} trend={signal['trend_score']} "
            f"momentum={signal['momentum_score']} relative={signal['relative_strength_score']} "
            f"risk={signal['risk_score']} etfs={','.join(signal['related_etfs'])}"
        )
        for group, drivers in (signal.get("drivers") or {}).items():
            if drivers:
                print(f"  driver[{group}]: {drivers[0]}")


if __name__ == "__main__":
    main()
