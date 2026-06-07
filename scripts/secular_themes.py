from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.secular_theme_engine import ALL_WINDOW_HOURS, generate_secular_theme_signals


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate secular theme signals.")
    parser.add_argument("--window-hours", type=int, default=24, help="Signal window in hours.")
    parser.add_argument("--all-windows", action="store_true", help="Run 7d, 30d, and 90d windows.")
    parser.add_argument("--dry-run", action="store_true", help="Print secular themes only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert secular theme signals into local PostgreSQL.")
    return parser.parse_args()


def print_signals(window_hours: int, signals: list[dict], *, dry_run: bool) -> None:
    print(f"Window {window_hours}h: generated {len(signals):,} secular theme signal(s)")
    if dry_run:
        print("[dry-run] secular theme signals were not written")
    print("Top secular themes:")
    for signal in sorted(signals, key=lambda row: row["secular_score"], reverse=True)[:8]:
        print(
            f"- theme={signal['theme_name']} parent={signal['parent_theme']} "
            f"secular={signal['secular_score']} tactical={signal['tactical_score']} "
            f"phase={signal['theme_phase']} confidence={signal['confidence']} "
            f"mentions_7d={signal['mention_count_7d']} mentions_30d={signal['mention_count_30d']} "
            f"evidence={signal.get('evidence_score', 0)} "
            f"etfs={','.join(signal['related_etfs'])}"
        )
        if signal.get("top_subthemes"):
            print(f"  matched={', '.join(signal['top_subthemes'][:5])}")
    print("Top tactical themes:")
    for signal in sorted(signals, key=lambda row: row["tactical_score"], reverse=True)[:5]:
        print(
            f"- theme={signal['theme_name']} tactical={signal['tactical_score']} "
            f"secular={signal['secular_score']} phase={signal['theme_phase']} "
            f"evidence={signal.get('evidence_score', 0)}"
        )
    print("Divergence examples:")
    divergences = [
        signal for signal in signals
        if abs(float(signal["secular_score"]) - float(signal["tactical_score"])) >= 12
    ]
    for signal in sorted(divergences, key=lambda row: abs(row["secular_score"] - row["tactical_score"]), reverse=True)[:5]:
        print(
            f"- theme={signal['theme_name']} secular={signal['secular_score']} "
            f"tactical={signal['tactical_score']} phase={signal['theme_phase']}"
        )
    print("Theme counts:")
    for signal in sorted(signals, key=lambda row: row["mention_count_30d"], reverse=True):
        print(
            f"- {signal['theme_name']}: 7d={signal['mention_count_7d']} "
            f"30d={signal['mention_count_30d']} 90d={signal['mention_count_90d']} "
            f"evidence={signal.get('evidence_score', 0)}"
        )


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    windows = ALL_WINDOW_HOURS if args.all_windows else [args.window_hours]
    engine = local_engine(use_insertmanyvalues=True)
    for window_hours in windows:
        signals = generate_secular_theme_signals(engine, window_hours=window_hours, dry_run=dry_run)
        print_signals(window_hours, signals, dry_run=dry_run)


if __name__ == "__main__":
    main()
