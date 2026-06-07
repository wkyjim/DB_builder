from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.sector_intelligence import generate_sector_intelligence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate local sector intelligence signals.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--dry-run", action="store_true", help="Print sector signals only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert sector signals into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine(use_insertmanyvalues=True)
    signals = generate_sector_intelligence(engine, window_hours=args.window_hours, dry_run=dry_run)

    print(f"Window {args.window_hours}h: generated {len(signals):,} sector signal(s)")
    if dry_run:
        print("[dry-run] sector signals were not written")
    for signal in signals[:20]:
        print(
            f"- rank={signal['rank']} sector={signal['sector_name']} "
            f"final={signal['final_score']} opportunity={signal['opportunity_score']} "
            f"risk={signal['risk_score']} momentum={signal['momentum_score']} "
            f"trend={signal['trend_score']} etfs={','.join(signal['related_etfs'])}"
        )
        for reason in signal.get("reasons", [])[:2]:
            print(f"  reason: {reason}")
        if signal["top_themes"]:
            print(f"  themes: {', '.join(signal['top_themes'][:5])}")


if __name__ == "__main__":
    main()
