from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.market_regime import generate_market_regime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local market regime signal.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--dry-run", action="store_true", help="Print regime only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert regime into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine(use_insertmanyvalues=True)
    signal = generate_market_regime(engine, window_hours=args.window_hours, dry_run=dry_run)

    print(f"Window {args.window_hours}h: generated market regime signal")
    if dry_run:
        print("[dry-run] market regime was not written")
    print(
        f"- regime={signal['regime_label']} "
        f"risk_on={signal['risk_on_score']} "
        f"risk_off={signal['risk_off_score']} "
        f"confidence={signal['confidence_score']} "
        f"news={signal['news_signal_count']} "
        f"macro={signal['macro_signal_count']}"
    )
    for driver in signal["drivers"][:10]:
        print(f"  driver: {driver}")


if __name__ == "__main__":
    main()
