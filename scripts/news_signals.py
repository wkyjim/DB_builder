from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.news_signal_aggregation import DEFAULT_WINDOWS, generate_news_signals


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate local classified news into investable signals.")
    parser.add_argument("--window-hours", type=int, default=24, help="Aggregation window in hours.")
    parser.add_argument("--all-windows", action="store_true", help="Run 6h, 24h, and 72h windows.")
    parser.add_argument("--dry-run", action="store_true", help="Print signals only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert signals into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    windows = DEFAULT_WINDOWS if args.all_windows else [args.window_hours]
    engine = local_engine(use_insertmanyvalues=True)

    for window_hours in windows:
        signals = generate_news_signals(engine, window_hours=window_hours, dry_run=dry_run)
        print(f"Window {window_hours}h: generated {len(signals):,} signal(s)")
        if dry_run:
            print("[dry-run] signals were not written")
        for signal in signals[:20]:
            print(
                f"- {signal['dimension_type']}={signal['dimension_value']} "
                f"articles={signal['article_count']} "
                f"weighted_sentiment={signal['weighted_sentiment_score']} "
                f"opportunity={signal['opportunity_score']} "
                f"risk={signal['risk_score']}"
            )


if __name__ == "__main__":
    main()
