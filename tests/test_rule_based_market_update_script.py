from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.rule_based_market_update import publish_to_dashboard


def test_build_cycle_metadata_block_localizes_utc_to_hkt():
    """Regression: a UTC-generated_at must render as HKT (UTC+8), not UTC.

    The 20:05 UTC → 04:05 HKT (next day) offset is the exact skew that
    produced the '12:05 HKT' bug in production.
    """
    from scripts.rule_based_market_update import build_cycle_metadata_block

    utc = datetime(2026, 9, 24, 20, 5, 31, tzinfo=timezone.utc)
    block = build_cycle_metadata_block("20260924_2000_HKT", None, utc)

    # HKT is UTC+8 → 20:05:31 UTC = 04:05:31 HKT on 25 September
    assert "Cycle ID: 20260924_2000_HKT" in block
    assert "25 September 2026, 04:05:31 (HKT)" in block
    assert "12:05" not in block  # the old bug


def test_build_cycle_metadata_block_naive_utc():
    """A naive (tz-unaware) datetime is assumed UTC and localized to HKT."""
    from scripts.rule_based_market_update import build_cycle_metadata_block

    naive_utc = datetime(2026, 9, 24, 12, 5, 31)  # naive, represents UTC
    block = build_cycle_metadata_block("C", None, naive_utc)

    # 12:05 UTC → 20:05 HKT
    assert "24 September 2026, 20:05:31 (HKT)" in block


def test_build_cycle_metadata_block_hkt_passthrough():
    """An already-HKT datetime must be preserved (no double conversion)."""
    from scripts.rule_based_market_update import build_cycle_metadata_block

    hkt = ZoneInfo("Asia/Hong_Kong")
    already_hkt = datetime(2026, 9, 24, 20, 5, 31, tzinfo=hkt)
    block = build_cycle_metadata_block("C", None, already_hkt)

    assert "24 September 2026, 20:05:31 (HKT)" in block


def test_build_cycle_metadata_block_source_meta():
    from scripts.rule_based_market_update import build_cycle_metadata_block

    utc = datetime(2026, 9, 24, 20, 5, 31, tzinfo=timezone.utc)
    meta = {
        "data_refresh_completed_at": "2026-09-24T15:00:00+08:00",
        "sources": {
            "us_equities": {"observation_date": "2026-09-23"},
            "news_classify": {"observation_date": None},
        },
        "degraded_sources": ["news_classify"],
    }
    block = build_cycle_metadata_block("20260924_2000_HKT", meta, utc)

    assert "Data refresh completed at: 2026-09-24T15:00:00+08:00" in block
    assert "Equity data through: 2026-09-23" in block
    assert "Degraded sources: news_classify" in block


def test_publish_to_dashboard_copies_latest_report(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nlatest content\n", encoding="utf-8")
    dashboard = tmp_path / "market-dashboard"
    (dashboard / ".git").mkdir(parents=True)

    target = publish_to_dashboard(report, dashboard, push=False)

    assert target == dashboard / "data" / "latest-report.md"
    assert target.read_text(encoding="utf-8") == "# Report\n\nlatest content\n"


def test_publish_to_dashboard_syncs_before_automated_commit(tmp_path: Path, monkeypatch):
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nlatest content\n", encoding="utf-8")
    dashboard = tmp_path / "market-dashboard"
    (dashboard / ".git").mkdir(parents=True)
    calls = []

    def fake_run(command, *, check):
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("scripts.rule_based_market_update.subprocess.run", fake_run)

    publish_to_dashboard(report, dashboard, push=True)

    assert calls[0] == (["git", "-C", str(dashboard), "fetch", "origin", "main"], True)
    assert calls[1] == (["git", "-C", str(dashboard), "merge", "--ff-only", "origin/main"], True)
    assert calls[2] == (["git", "-C", str(dashboard), "add", "data/latest-report.md"], True)
    assert calls[3] == (["git", "-C", str(dashboard), "diff", "--cached", "--quiet"], False)

def test_hourly_pipeline_ps1_no_cmd_call_activate():
    """Regression: the PowerShell orchestrator must never contain CMD-only
    'call ...activate.bat' syntax. 'call' is a cmd.exe built-in that is
    invalid in PowerShell and caused a dry-run failure at line 45.

    The fix: replace 'call ...activate.bat' with an explicit $PythonExe
    variable (C:\\Users\\User\\anaconda3\\envs\\PostgreSQL_db\\python.exe).
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # Must not contain the CMD-only 'call ...activate.bat' construct
    assert 'call "C:\\Users\\User\\anaconda3\\Scripts\\activate.bat"' not in content
    # More generally, 'call' as a standalone command should not appear
    import re
    # Match 'call' as a word at the start of a line (PowerShell CMD syntax)
    cmd_call_pattern = re.compile(r'^\s*call\s', re.MULTILINE)
    assert not cmd_call_pattern.search(content), "CMD-only 'call' command found in .ps1 file"


def test_hourly_pipeline_ps1_join_uses_global_timeout():
    """Regression: the join must use a single Wait-Job across ALL branches
    with one global timeout, NOT sequential per-job Wait-Job calls.

    Old bug: foreach ($Job in $BranchJobs) { Wait-Job -Job $Job -Timeout 2400 }
    meant 5 jobs × 2400s = 12000s max wall-clock.

    Fixed: Wait-Job -Job $Jobs -Timeout $OverallTimeoutSeconds (600s shared).
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # Must NOT have foreach loop with per-job Wait-Job
    assert 'foreach ($Job in $BranchJobs)' not in content, "Sequential per-job Wait-Job still present"
    # Must NOT have pipe from Wait-Job to Receive-Job
    assert 'Wait-Job -Job $Job -Timeout' not in content, "Per-job Wait-Job still present"
    # Must have global Wait-Job
    assert 'Wait-Job -Job $Jobs -Timeout' in content, "Global Wait-Job not found"
    # Must have $OverallTimeoutSeconds defined
    assert '$OverallTimeoutSeconds' in content, "$OverallTimeoutSeconds not defined"


def test_hourly_pipeline_ps1_join_stops_timed_out_jobs():
    """Regression: timed-out jobs must be stopped via Stop-Job, not left running."""
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert 'Stop-Job -Job $Job -Force' in content, "Stop-Job not found for timed-out jobs"
    assert 'JobStateInfo.State' in content, "JobStateInfo.State not checked"
    assert 'Stopwatch' in content and '.Elapsed.TotalSeconds' in content, "Stopwatch not used for timing"


def test_hourly_pipeline_ps1_join_captures_errors():
    """Regression: background-job errors must be captured, not silently ignored."""
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert 'ChildJobs[0].Error' in content, "ChildJobs[0].Error not captured"


def test_hourly_pipeline_ps1_all_branches_receive_dryrun():
    """Regression: every Start-Job must pass $DryRun via -ArgumentList."""
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    import re
    arg_lines = re.findall(r'-ArgumentList.*', content)
    # Count 5 branches and all must have $DryRun
    dryrun_arg_count = sum(1 for l in arg_lines if '$DryRun' in l)
    assert dryrun_arg_count >= 5, f"Only {dryrun_arg_count}/5 branches pass $DryRun"


def test_hourly_pipeline_ps1_no_datetime_subtraction_hazard():
    """Regression: Get-Date must never be used in arithmetic subtraction.

    PowerShell parses "(Get-Date - $var)" as Get-Date receiving
    "-$var" as parameters, causing a runtime parse error.

    Safe patterns:
    - (Get-Date) - $var   (parenthesized command, then subtraction)
    - [System.Diagnostics.Stopwatch]::StartNew() (preferred)
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # Must NOT have Get-Date followed by - as arithmetic subtraction
    import re
    # Match "(Get-Date -" pattern — the hazardous one
    hazard_pattern = re.compile(r'\(Get-Date\s+-\s')
    hazards = hazard_pattern.findall(content)
    assert not hazards, f"DateTime subtraction hazard found: {hazards}. Use Stopwatch or (Get-Date) - $var instead."

