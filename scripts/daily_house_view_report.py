from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.investment_report import generate_investment_report, save_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the daily institutional house view report.")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--with-qwen-overlay", action="store_true")
    parser.add_argument("--no-qwen-overlay", action="store_true")
    parser.add_argument("--qwen-timeout", type=int, default=120)
    parser.add_argument("--include-appendix", action="store_true")
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
        with_qwen_overlay=args.with_qwen_overlay and not args.no_qwen_overlay,
        qwen_timeout=args.qwen_timeout,
        report_type="daily",
        include_appendix=args.include_appendix,
    )
    if args.save:
        path = save_report(markdown, prefix="daily_house_view")
        print(f"Saved daily house view report: {path}")
        return
    print(markdown)


if __name__ == "__main__":
    main()
