from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.deepseek_cio_engine import (
    generate_cio_report,
    prompt_preview,
    save_deepseek_cio_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a manual DeepSeek CIO house-view report.")
    parser.add_argument("--window-hours", type=int, default=24, help="Input signal window in hours.")
    parser.add_argument("--timeout", type=int, default=900, help="DeepSeek/Ollama timeout in seconds.")
    parser.add_argument("--model", default=None, help="Override OLLAMA_DEEP_MODEL for this run.")
    parser.add_argument("--dry-run", action="store_true", help="Build snapshot and prompt; do not save.")
    parser.add_argument("--save", action="store_true", help="Call DeepSeek and save the markdown report.")
    parser.add_argument("--call-model", action="store_true", help="Call DeepSeek even in dry-run mode.")
    parser.add_argument("--evidence-locked", dest="evidence_locked", action="store_true", default=True, help="Require evidence IDs and strict snapshot grounding.")
    parser.add_argument("--no-evidence-lock", dest="evidence_locked", action="store_false", help="Experimental: bypass evidence-lock validation.")
    parser.add_argument("--include-raw-snapshot", action="store_true", help="Print the compact structured snapshot.")
    parser.add_argument("--include-raw-response", action="store_true", help="Include raw invalid model output in fallback diagnostics.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dry_run and not args.save:
        args.dry_run = True

    engine = local_engine(use_insertmanyvalues=True)
    result = generate_cio_report(
        engine,
        window_hours=args.window_hours,
        timeout=args.timeout,
        model=args.model,
        call_model=args.call_model or args.save,
        include_raw_response=args.include_raw_response,
        evidence_locked=args.evidence_locked,
    )

    print(f"DeepSeek CIO status: {result['status']}")
    print("")
    print("# Prompt Preview")
    print("")
    print(prompt_preview(result["messages"], max_chars=5000))

    if args.include_raw_snapshot:
        print("")
        print("# Raw Snapshot")
        print("")
        print(json.dumps(result["snapshot"], indent=2, ensure_ascii=True, default=str))

    if result["markdown"]:
        print("")
        print("# Report Preview")
        print("")
        print(result["markdown"])

    if args.save:
        path = save_deepseek_cio_report(result["markdown"])
        print(f"Saved DeepSeek CIO report: {path}")


if __name__ == "__main__":
    main()
