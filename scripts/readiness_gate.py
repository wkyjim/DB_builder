"""Deterministic readiness gate for downstream Market Intelligence HTML runs.

Verifies, BEFORE the HTML report is generated, that the hourly full-refresh
cycle produced a current, valid latest-report.md:

  A. state file exists, overall_status == REPORT_READY
  B. cycle_id is recent (within max_cycle_age_minutes of now, default 65)
  C. latest-report.md on-disk SHA-256 == state.latest_report.sha256
  D. latest-report.md embeds the cycle_id (metadata block)
  E. CRITICAL sources all OK
  F. report structure validates (non-empty, key sections, generated-at line)

Git publication is reported SEPARATELY: a valid local report with a failed
push yields READY_WITH_PUBLICATION_WARNING, not a stale-data block.

Exit codes:
  0  READY or READY_WITH_PUBLICATION_WARNING (prints which)
  2  UPSTREAM_NOT_READY (prints failed stage)
  3  usage/config error
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (puts src/ on sys.path)

import sys

# pipeline_state lives in scripts/ (sibling), not the db_builder package
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pipeline_state import (  # noqa: E402
    CRITICAL_SOURCES,
    STATE_PATH,
    load_state,
    now_hkt,
    sha256_file,
)

DASHBOARD_REPORT = (
    Path(__file__).resolve().parents[1] / "market-dashboard" / "data" / "latest-report.md"
)

REQUIRED_REPORT_MARKERS = [
    "# Rule-Based Institutional Market Update",
    "Generated at:",
    "Window:",
]


def parse_cycle_timestamp(cycle_id: str):
    m = re.match(r"^(\d{8})_(\d{4})_HKT$", cycle_id or "")
    if not m:
        return None
    from datetime import datetime

    try:
        return datetime.strptime(
            f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M"
        ).replace(tzinfo=now_hkt().tzinfo)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Market Intelligence readiness gate")
    parser.add_argument(
        "--report",
        type=Path,
        default=DASHBOARD_REPORT,
        help="Path to latest-report.md",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=STATE_PATH,
        help="Path to latest_report_status.json",
    )
    parser.add_argument(
        "--max-cycle-age-minutes",
        type=float,
        default=65.0,
        help="Maximum age of the cycle_id before the report is considered stale",
    )
    args = parser.parse_args()

    def not_ready(stage: str, detail: str = "") -> int:
        print(f"UPSTREAM_NOT_READY: {stage}" + (f" | {detail}" if detail else ""))
        return 2

    # A. state file + status
    state = load_state(args.state)
    if state is None:
        return not_ready("STATE_MISSING", str(args.state))
    if state.get("overall_status") != "REPORT_READY":
        return not_ready(
            "CYCLE_NOT_REPORT_READY",
            f"cycle={state.get('cycle_id')} status={state.get('overall_status')}",
        )

    # B. cycle recency
    cycle_id = state.get("cycle_id") or ""
    cycle_ts = parse_cycle_timestamp(cycle_id)
    if cycle_ts is None:
        return not_ready("CYCLE_ID_UNPARSABLE", cycle_id)
    age_minutes = (now_hkt() - cycle_ts).total_seconds() / 60.0
    if age_minutes > args.max_cycle_age_minutes:
        return not_ready(
            "CYCLE_STALE",
            f"cycle={cycle_id} age={age_minutes:.0f}m > {args.max_cycle_age_minutes:.0f}m",
        )
    if age_minutes < -10:
        return not_ready("CYCLE_ID_IN_FUTURE", cycle_id)

    # C. report file + SHA match
    report = args.report
    if not report.is_file():
        return not_ready("REPORT_MISSING", str(report))
    actual_sha = sha256_file(report)
    expected_sha = (state.get("latest_report") or {}).get("sha256")
    if expected_sha and actual_sha != expected_sha:
        return not_ready(
            "REPORT_SHA_MISMATCH",
            f"expected={expected_sha[:12]} actual={actual_sha[:12]}",
        )

    # D. cycle_id embedded in report metadata
    text = report.read_text(encoding="utf-8", errors="replace")
    if cycle_id not in text:
        return not_ready("REPORT_CYCLE_ID_MISSING", cycle_id)

    # E. critical sources
    sources = state.get("sources") or {}
    failed_critical = [
        name
        for name in CRITICAL_SOURCES
        if (sources.get(name) or {}).get("status") != "OK"
    ]
    if failed_critical:
        return not_ready("CRITICAL_SOURCE_NOT_OK", ",".join(failed_critical))

    # F. report structure
    missing_markers = [m for m in REQUIRED_REPORT_MARKERS if m not in text]
    if missing_markers:
        return not_ready("REPORT_STRUCTURE_INVALID", ",".join(missing_markers))
    if len(text.strip()) < 5000:
        return not_ready("REPORT_TOO_SHORT", f"{len(text.strip())} chars")

    # Publication state — reported, not gating (unless user demands push)
    pub = state.get("publication_status")
    git = state.get("git") or {}
    if pub == "PUSHED" and git.get("head_equals_origin"):
        print(f"READY | cycle={cycle_id} | publication=PUSHED | commit={git.get('commit')}")
        return 0
    print(
        f"READY_WITH_PUBLICATION_WARNING | cycle={cycle_id} | "
        f"publication={pub} | git_error={git.get('error')} | "
        "local report is current and valid; GitHub publication did not confirm"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
