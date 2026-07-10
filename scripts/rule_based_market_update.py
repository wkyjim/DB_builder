from __future__ import annotations

import argparse
import shutil
import subprocess
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
    parser.add_argument(
        "--publish-dashboard",
        action="store_true",
        help="Copy the saved report to market-dashboard/data/latest-report.md.",
    )
    parser.add_argument(
        "--dashboard-repo",
        type=Path,
        default=Path(r"C:\Users\User\OneDrive\Coding\market-dashboard"),
        help="Local market-dashboard repository path.",
    )
    parser.add_argument(
        "--push-dashboard",
        action="store_true",
        help="Commit and push the updated market-dashboard latest report.",
    )
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args()


def publish_to_dashboard(report_path: Path, dashboard_repo: Path, *, push: bool = False) -> Path:
    if not dashboard_repo.exists():
        raise FileNotFoundError(f"Dashboard repository not found: {dashboard_repo}")
    if not (dashboard_repo / ".git").exists():
        raise FileNotFoundError(f"Dashboard path is not a git repository: {dashboard_repo}")

    target = dashboard_repo / "data" / "latest-report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_path, target)
    print(f"Published latest report to dashboard: {target}")

    if push:
        subprocess.run(["git", "-C", str(dashboard_repo), "add", "data/latest-report.md"], check=True)
        diff_result = subprocess.run(
            ["git", "-C", str(dashboard_repo), "diff", "--cached", "--quiet"],
            check=False,
        )
        if diff_result.returncode == 0:
            print("Dashboard latest report unchanged; nothing to commit.")
        else:
            subprocess.run(
                ["git", "-C", str(dashboard_repo), "commit", "-m", "Update latest market report"],
                check=True,
            )
            subprocess.run(["git", "-C", str(dashboard_repo), "push", "origin", "main"], check=True)
            print("Pushed latest report to market-dashboard.")

    return target


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
        if args.publish_dashboard:
            publish_to_dashboard(path, args.dashboard_repo, push=args.push_dashboard and not args.dry_run)
        return
    if args.publish_dashboard:
        raise ValueError("--publish-dashboard requires --save")
    print(markdown)


if __name__ == "__main__":
    main()
