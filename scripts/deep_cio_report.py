from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.investment_report import generate_investment_report, save_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a manual deep CIO investment report.")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--with-qwen-overlay", action="store_true")
    parser.add_argument("--with-deepseek-cio", action="store_true")
    parser.add_argument("--qwen-timeout", type=int, default=120)
    parser.add_argument("--deepseek-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    markdown = generate_investment_report(
        engine,
        window_hours=args.window_hours,
        quality_summary=False,
        with_qwen_overlay=args.with_qwen_overlay,
        with_deepseek_cio=args.with_deepseek_cio,
        qwen_timeout=args.qwen_timeout,
        deepseek_timeout=args.deepseek_timeout,
    )
    if args.save:
        path = save_report(markdown)
        print(f"Saved deep CIO report: {path}")
    else:
        print(markdown)


if __name__ == "__main__":
    main()
