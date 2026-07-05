from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.deepseek_offline_agent import generate_offline_report, prompt_preview, save_offline_report


def section_prompt_preview(section_messages: list[dict], *, verbose: bool = False) -> str:
    selected = section_messages if verbose else section_messages[:2]
    chunks = []
    for item in selected:
        chunks.append(f"# {item['heading'].replace('## ', '')}\n")
        chunks.append(prompt_preview(item["messages"], max_chars=5000))
    if not verbose and len(section_messages) > len(selected):
        chunks.append(f"\n...[{len(section_messages) - len(selected)} section prompts omitted; use --verbose to show all]...")
    return "\n\n".join(chunks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an offline DeepSeek CIO report from local data only.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--call-model", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--model", default=None)
    parser.add_argument("--include-snapshot", action="store_true")
    parser.add_argument("--include-prompt", action="store_true")
    parser.add_argument("--include-raw-response", action="store_true")
    parser.add_argument("--no-evidence-lock", action="store_true")
    parser.add_argument("--section-mode", dest="section_mode", action="store_true", default=True)
    parser.add_argument("--no-section-mode", dest="section_mode", action="store_false")
    parser.add_argument("--max-section-retries", type=int, default=1)
    parser.add_argument("--section-timeout", type=int, default=180)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dry_run and not args.save:
        args.dry_run = True
    engine = local_engine(use_insertmanyvalues=True)
    result = generate_offline_report(
        engine,
        window_hours=args.window_hours,
        call_model=args.call_model or args.save,
        timeout=args.timeout,
        model=args.model,
        evidence_locked=not args.no_evidence_lock,
        include_raw_response=args.include_raw_response,
        section_mode=args.section_mode,
        section_timeout=args.section_timeout,
        max_section_retries=args.max_section_retries,
    )
    print(f"DeepSeek offline report status: {result['status']}")
    print(f"Retrieved knowledge chunks: {len(result['knowledge_chunks'])}")
    if result.get("section_mode"):
        print(f"Valid DeepSeek sections: {result.get('valid_sections', 0)}")
        print(f"Fallback sections: {result.get('fallback_sections', 0)}")

    if args.include_snapshot:
        print("\n# Snapshot\n")
        print(json.dumps(result["snapshot"], indent=2, ensure_ascii=True, default=str))
    if args.include_prompt:
        print("\n# Prompt Preview\n")
        if result.get("section_mode"):
            print(section_prompt_preview(result["messages"], verbose=args.verbose))
        else:
            print(prompt_preview(result["messages"]))
    if result["markdown"]:
        print("\n# Report Preview\n")
        print(result["markdown"])

    if args.save:
        if result["status"] == "valid":
            path = save_offline_report(result["markdown"])
            print(f"Saved DeepSeek offline report: {path}")
        else:
            path = save_offline_report(result["markdown"], diagnostics=True)
            print(f"Saved DeepSeek offline diagnostics: {path}")
            print(f"Validation failure: {result['reason']}")


if __name__ == "__main__":
    main()
