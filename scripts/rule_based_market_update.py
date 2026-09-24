from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.report_renderer import render_rule_based_market_update, save_rule_based_report, score_all, scores_to_json
from db_builder.rule_based_market_data import collect_rule_based_inputs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic rule-based market update report.")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument(
        "--cycle-id",
        default=None,
        help="Pipeline cycle identifier (e.g. 20260923_0800_HKT) embedded in report metadata.",
    )
    parser.add_argument(
        "--source-meta-json",
        type=Path,
        default=None,
        help="Optional JSON file with per-source observation dates/degradation recorded by the orchestrator.",
    )
    parser.add_argument(
        "--publish-dashboard",
        action="store_true",
        help="Copy the saved report to market-dashboard/data/latest-report.md.",
    )
    parser.add_argument(
        "--dashboard-repo",
        type=Path,
        default=PROJECT_ROOT / "market-dashboard",
        help="Local market-dashboard repository path.",
    )
    parser.add_argument(
        "--push-dashboard",
        action="store_true",
        help="Commit and push the updated market-dashboard latest report.",
    )
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="Send a Telegram report-update summary after the report is saved or published.",
    )
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args()


def build_cycle_metadata_block(cycle_id: str | None, source_meta: dict | None, generated_at) -> str:
    """Machine-readable cycle/source metadata appended to the full report.

    Preserves every existing report section — this block is ADDITIVE. All
    execution timestamps are HKT. observation_date (what the data is current
    through) is distinguished from retrieved_at (when we checked it).

    generated_at may be timezone-naive UTC (legacy) or tz-aware; it is
    localized to Asia/Hong_Kong before formatting so the metadata block
    matches the report header's HKT timestamp exactly.
    """
    from datetime import timezone as _timezone
    from zoneinfo import ZoneInfo

    if generated_at is None:
        from datetime import datetime as _datetime

        generated_at = _datetime.now(_timezone.utc)
    if generated_at.tzinfo is None:
        # Legacy callers pass naive UTC timestamps.
        generated_at = generated_at.replace(tzinfo=_timezone.utc)
    generated_at_hkt = generated_at.astimezone(ZoneInfo("Asia/Hong_Kong"))

    lines = ["", "## Cycle Metadata", ""]
    if cycle_id:
        lines.append(f"- Cycle ID: {cycle_id}")
    lines.append(f"- Report generated at: {generated_at_hkt:%d %B %Y, %H:%M:%S} (HKT)")
    if source_meta:
        refresh_completed = source_meta.get("data_refresh_completed_at")
        if refresh_completed:
            lines.append(f"- Data refresh completed at: {refresh_completed}")
        label_map = {
            "us_equities": "Equity data through",
            "macro_rates": "Rates data through",
            "macro_fx_commodities": "FX data through",
            "macro_fx_commodities_commodities": "Commodities data through",
            "macro_market_indices": "Macro-market data through",
            "economic_releases": "Economic releases through",
            "etf_flows": "ETF flow data through",
            "finra_short": "FINRA data through",
            "news_fetch": "News data through",
            "asia_futures": "Asia futures data through",
        }
        sources = source_meta.get("sources") or {}
        for key, label in label_map.items():
            entry = sources.get(key)
            if not entry:
                continue
            obs = entry.get("observation_date") or "unknown"
            note = f" ({entry['note']})" if entry.get("note") else ""
            lines.append(f"- {label}: {obs}{note}")
        degraded = source_meta.get("degraded_sources") or []
        if degraded:
            lines.append(f"- Degraded sources: {', '.join(degraded)}")
        else:
            lines.append("- Degraded sources: none")
    lines.append("")
    return "\n".join(lines)


def sync_dashboard_repo(dashboard_repo: Path) -> None:
    """Fast-forward the dashboard branch before creating an automated report commit."""
    subprocess.run(
        ["git", "-C", str(dashboard_repo), "fetch", "origin", "main"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dashboard_repo), "merge", "--ff-only", "origin/main"],
        check=True,
    )


def publish_to_dashboard(report_path: Path, dashboard_repo: Path, *, push: bool = False, dry_run: bool = False) -> Path:
    if not dashboard_repo.exists():
        raise FileNotFoundError(f"Dashboard repository not found: {dashboard_repo}")
    if not (dashboard_repo / ".git").exists():
        raise FileNotFoundError(f"Dashboard path is not a git repository: {dashboard_repo}")

    target = dashboard_repo / "data" / "latest-report.md"
    if dry_run:
        print(f"[DRY RUN] Would publish latest report to dashboard: {target}")
        if push:
            print("[DRY RUN] Would commit and push data/latest-report.md to market-dashboard.")
        return target

    if push:
        sync_dashboard_repo(dashboard_repo)

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


def atomic_publish_to_dashboard(report_path: Path, dashboard_repo: Path) -> Path:
    """Atomically replace latest-report.md (temp write + os.replace)."""
    import os
    import tempfile

    target = dashboard_repo / "data" / "latest-report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".latest-report.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as out, open(report_path, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    print(f"Atomically published latest report to dashboard: {target}")
    return target


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    data = collect_rule_based_inputs(engine, window_hours=args.window_hours)
    scores = score_all(data)
    markdown = render_rule_based_market_update(data, scores)

    generated_at = data.get("generated_at")
    source_meta = None
    if args.source_meta_json and args.source_meta_json.is_file():
        import json

        source_meta = json.loads(args.source_meta_json.read_text(encoding="utf-8"))
    markdown = markdown + build_cycle_metadata_block(args.cycle_id, source_meta, generated_at)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(scores_to_json(scores), encoding="utf-8")
    if args.save:
        path = save_rule_based_report(markdown, generated_at=generated_at)
        print(f"Saved rule-based market update report: {path}")
        notification_path = path
        if args.publish_dashboard and not args.dry_run:
            notification_path = atomic_publish_to_dashboard(path, args.dashboard_repo)
            if args.push_dashboard:
                _commit_and_push_dashboard(args.dashboard_repo)
        elif args.publish_dashboard:
            print(f"[DRY RUN] Would atomically publish latest report to dashboard: {args.dashboard_repo / 'data' / 'latest-report.md'}")
            if args.push_dashboard:
                print("[DRY RUN] Would commit and push data/latest-report.md to market-dashboard.")
        if args.notify_telegram:
            from db_builder.telegram_bot import build_report_update_summary, send_telegram_message

            message = build_report_update_summary(markdown, report_path=notification_path)
            send_telegram_message(message, dry_run=args.dry_run)
            print("Telegram report update notification prepared." if args.dry_run else "Telegram report update notification sent.")
        return
    if args.publish_dashboard:
        raise ValueError("--publish-dashboard requires --save")
    print(markdown)


def _commit_and_push_dashboard(dashboard_repo: Path) -> None:
    """Commit and push the dashboard latest report. Never force-push.

    Push failure is NON-FATAL by design (REPORT_READY vs PUBLISHED
    separation): a failed push prints a warning and the script still exits
    0, so callers do not retry the whole report generation — retrying would
    regenerate + recommit duplicate reports. The next cycle's
    sync_dashboard_repo + push publishes the outstanding commit.
    """
    subprocess.run(
        ["git", "-C", str(dashboard_repo), "add", "data/latest-report.md"],
        check=True,
    )
    diff_result = subprocess.run(
        ["git", "-C", str(dashboard_repo), "diff", "--cached", "--quiet"],
        check=False,
    )
    if diff_result.returncode == 0:
        print("Dashboard latest report unchanged; nothing to commit.")
        return
    subprocess.run(
        ["git", "-C", str(dashboard_repo), "commit", "-m", "Update latest market report"],
        check=True,
    )
    push_result = subprocess.run(
        ["git", "-C", str(dashboard_repo), "push", "origin", "main"],
        check=False,
    )
    if push_result.returncode == 0:
        print("Pushed latest report to market-dashboard.")
    else:
        print(
            "[PUSH FAILED] git push exited "
            f"{push_result.returncode}; the commit is local and the report is "
            "REPORT_READY. Publication will be completed by the next cycle's "
            "sync+push. NOT retrying report generation (prevents duplicate "
            "commits)."
        )


if __name__ == "__main__":
    main()
