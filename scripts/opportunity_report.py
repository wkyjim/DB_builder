from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.opportunity_scanner import generate_opportunity_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate local ticker opportunity signals.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument(
        "--universe",
        choices=["watchlist", "etf", "all"],
        default="watchlist",
        help="Ticker universe to scan.",
    )
    parser.add_argument("--include-meme", action="store_true", help="Include meme/high-noise tickers.")
    parser.add_argument("--dry-run", action="store_true", help="Print report only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert opportunities into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine(use_insertmanyvalues=True)
    signals = generate_opportunity_report(
        engine,
        window_hours=args.window_hours,
        dry_run=dry_run,
        universe=args.universe,
        include_meme=args.include_meme,
    )

    print(
        f"Window {args.window_hours}h: generated {len(signals):,} opportunity signal(s) "
        f"universe={args.universe} include_meme={args.include_meme}"
    )
    if dry_run:
        print("[dry-run] opportunity signals were not written")
    for signal in signals[:20]:
        print(
            f"- {signal['ticker']} label={signal['signal_label']} "
            f"opportunity={signal['opportunity_score']} "
            f"risk={signal['risk_score']} "
            f"technical={signal['technical_score']} "
            f"latest_date={signal['latest_date']}"
        )
        for reason in signal["reasons"][:3]:
            print(f"  reason: {reason}")


if __name__ == "__main__":
    main()
