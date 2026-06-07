from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.market_regime_v2 import generate_market_regime_v2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a market regime 2.0 signal.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--dry-run", action="store_true", help="Print regime only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert regime into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine(use_insertmanyvalues=True)
    signal = generate_market_regime_v2(engine, window_hours=args.window_hours, dry_run=dry_run)

    print(f"Window {args.window_hours}h: generated market regime 2.0 signal")
    if dry_run:
        print("[dry-run] market regime 2.0 was not written")
    print(
        f"- regime={signal['market_regime']} phase={signal['market_phase']} "
        f"confidence={signal['confidence']} bullish={signal['bullish_score']} "
        f"bearish={signal['bearish_score']} trend={signal['trend_state']} "
        f"momentum={signal['momentum_state']} volatility={signal['volatility_state']} "
        f"breadth={signal['breadth_state']} risk_appetite={signal['risk_appetite_state']}"
    )
    for group, drivers in (signal.get("drivers") or {}).items():
        for driver in drivers[:2]:
            print(f"  driver[{group}]: {driver}")


if __name__ == "__main__":
    main()
