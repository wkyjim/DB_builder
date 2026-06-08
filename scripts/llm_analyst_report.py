from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.llm_analyst_overlay import (
    generate_qwen_overlay,
    render_qwen_overlay_markdown,
    setup_llm_analyst_schema,
    upsert_llm_output,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate optional local LLM analyst overlay.")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--qwen-timeout", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    overlay = generate_qwen_overlay(engine, window_hours=args.window_hours, timeout=args.qwen_timeout)
    print(render_qwen_overlay_markdown(overlay))
    if args.upsert_local and not args.dry_run:
        setup_llm_analyst_schema(engine)
        upsert_llm_output(
            engine,
            window_hours=args.window_hours,
            analysis_type="qwen_regime_review",
            model_name=overlay.get("model_name", "qwen2.5:7b"),
            model_role="fast_structured_analysis",
            payload=overlay,
        )
        print("Stored Qwen analyst overlay.")
    else:
        print("[dry-run] LLM analyst overlay was not written")


if __name__ == "__main__":
    main()
