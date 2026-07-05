from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.investment_report import generate_investment_report, save_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local markdown investment intelligence report.")
    parser.add_argument(
        "--report-type",
        choices=["pulse", "daily", "house_view", "legacy"],
        default="daily",
        help="Report format to render. Default is daily institutional house view.",
    )
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--summary-timeout", type=int, default=120, help="DeepSeek/Ollama summary timeout in seconds.")
    parser.add_argument("--no-quality-summary", action="store_true", help="Skip DeepSeek report quality summary.")
    parser.add_argument("--with-qwen-overlay", action="store_true", help="Append optional Qwen analyst overlay.")
    parser.add_argument("--no-qwen-overlay", action="store_true", help="Disable Qwen overlay if enabled by wrapper scripts.")
    parser.add_argument("--with-deepseek-cio", action="store_true", help="Append optional DeepSeek CIO commentary.")
    parser.add_argument("--qwen-timeout", type=int, default=120, help="Qwen overlay timeout in seconds.")
    parser.add_argument("--deepseek-timeout", type=int, default=300, help="DeepSeek CIO timeout in seconds.")
    parser.add_argument("--include-appendix", action="store_true", help="Include raw diagnostic appendix.")
    parser.add_argument("--dry-run", action="store_true", help="Print markdown report; do not save.")
    parser.add_argument("--save", action="store_true", help="Save report under reports/.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    markdown = generate_investment_report(
        engine,
        window_hours=args.window_hours,
        quality_summary=not args.no_quality_summary,
        timeout=args.summary_timeout,
        with_qwen_overlay=args.with_qwen_overlay and not args.no_qwen_overlay,
        with_deepseek_cio=args.with_deepseek_cio,
        qwen_timeout=args.qwen_timeout,
        deepseek_timeout=args.deepseek_timeout,
        report_type=args.report_type,
        include_appendix=args.include_appendix,
    )

    if args.save:
        prefix = "market_pulse" if args.report_type == "pulse" else "daily_house_view"
        if args.report_type == "legacy":
            prefix = "investment_report"
        path = save_report(markdown, prefix=prefix)
        print(f"Saved investment report: {path}")
        return

    print(markdown)


if __name__ == "__main__":
    main()
