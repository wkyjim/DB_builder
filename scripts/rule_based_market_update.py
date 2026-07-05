from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.report_renderer import render_rule_based_market_update, save_rule_based_report, score_all, scores_to_json
from db_builder.rule_based_market_data import collect_rule_based_inputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic rule-based market update report.")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    data = collect_rule_based_inputs(engine, window_hours=args.window_hours)
    scores = score_all(data)
    markdown = render_rule_based_market_update(data, scores)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(scores_to_json(scores), encoding="utf-8")
    if args.save:
        path = save_rule_based_report(markdown, generated_at=data.get("generated_at"))
        print(f"Saved rule-based market update report: {path}")
        return
    print(markdown)


if __name__ == "__main__":
    main()
