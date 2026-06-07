from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.investment_report import generate_investment_report, save_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local markdown investment intelligence report.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--dry-run", action="store_true", help="Print markdown report; do not save.")
    parser.add_argument("--save", action="store_true", help="Save report under reports/.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    markdown = generate_investment_report(engine, window_hours=args.window_hours)

    if args.save:
        path = save_report(markdown)
        print(f"Saved investment report: {path}")
        return

    print(markdown)


if __name__ == "__main__":
    main()
