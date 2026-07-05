from __future__ import annotations

import argparse
import json
import time

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.deepseek_multistage_agent import (
    DEFAULT_MULTISTAGE_MODEL,
    OllamaSettings,
    run_multistage_report,
    save_multistage_report,
)
from db_builder.deepseek_stage_prompts import prompts_preview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a multistage offline DeepSeek CIO report.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--call-model", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--stage-timeout", type=int, default=240)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--top-p", type=float, default=0.85)
    parser.add_argument("--repeat-penalty", type=float, default=1.1)
    parser.add_argument("--num-ctx", type=int, default=None)
    parser.add_argument("--include-snapshot", action="store_true")
    parser.add_argument("--include-prompts", action="store_true")
    parser.add_argument("--include-stage-json", action="store_true")
    parser.add_argument("--allow-warnings", action="store_true")
    return parser.parse_args()


def main() -> None:
    started = time.perf_counter()
    args = parse_args()
    if not args.dry_run and not args.save:
        args.dry_run = True
    settings = OllamaSettings(
        model=args.model or DEFAULT_MULTISTAGE_MODEL,
        timeout=args.timeout,
        stage_timeout=args.stage_timeout,
        temperature=args.temperature,
        top_p=args.top_p,
        repeat_penalty=args.repeat_penalty,
        num_ctx=args.num_ctx,
    )
    engine = local_engine(use_insertmanyvalues=True)
    result = run_multistage_report(
        engine,
        window_hours=args.window_hours,
        call_model=args.call_model or args.save,
        settings=settings,
        include_stage_json=args.include_stage_json,
        allow_warnings=args.allow_warnings,
    )
    elapsed = time.perf_counter() - started
    print(f"Qwen multistage report status: {result['status']}")
    print(f"Model: {settings.model}")
    print(f"Elapsed seconds: {elapsed:.2f}")
    print(f"Stages: {len(result['stage_results'])}")
    print(f"Warnings: {len(result['warnings'])}")
    for stage in result["stage_results"]:
        print(f"- {stage['stage']}: {stage['status']}")

    if args.include_snapshot:
        print("\n# Snapshot\n")
        print(json.dumps(result["snapshot"], indent=2, ensure_ascii=True, default=str))
    if args.include_prompts:
        print("\n# Prompt Preview\n")
        print(prompts_preview(result["prompts"]))
    if args.include_stage_json:
        print("\n# Stage JSON\n")
        print(json.dumps(result["stage_outputs"], indent=2, ensure_ascii=True, default=str))
    if result["markdown"]:
        print("\n# Report Preview\n")
        print(result["markdown"])

    if args.save:
        if result["status"] == "valid":
            path = save_multistage_report(result["markdown"])
            print(f"Saved Qwen multistage report: {path}")
        else:
            if args.include_stage_json:
                path = save_multistage_report(result["markdown"], diagnostics=True)
                print(f"Saved Qwen multistage diagnostics: {path}")
            print(f"Final report not saved: {result['reason'] or 'validation/audit failed'}")


if __name__ == "__main__":
    main()
