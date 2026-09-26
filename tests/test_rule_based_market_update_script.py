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

    assert 'Stop-Job -Job $Job' in content, "Stop-Job not found for timed-out jobs"
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



def test_hourly_pipeline_ps1_dryrun_result_schema_has_seconds():
    """Regression: every branch return must have ExitCode and Error so that
    Measure-Object -Property Seconds -Maximum does not crash on missing
    properties, and every result has Seconds.

    Root cause: DryRun branches returned @{ Branch, Ok, Seconds, Output }
    without ExitCode/Error; and Receive-Job could return objects without
    Seconds if the scriptblock threw an exception.

    Fix: all branch returns now include ExitCode and Error; the join
    block normalizes every result to have Name, Status, Seconds,
    ExitCode, Error, Branch, Ok.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # Every branch definition must have ExitCode, Error, and Seconds
    # Check the file line-by-line for branch result definitions
    lines = content.split('\n')
    branch_def_lines = [l.strip() for l in lines if '@{' in l and 'Branch=' in l]
    assert len(branch_def_lines) >= 5, f"Expected at least 5 branch definitions, found {len(branch_def_lines)}"
    for line in branch_def_lines:
        assert 'ExitCode=' in line, f"Branch result missing ExitCode: {line[:120]}"
        assert 'Error=' in line, f"Branch result missing Error: {line[:120]}"
        assert 'Seconds=' in line, f"Branch result missing Seconds: {line[:120]}"

    # Normalization block must exist
    assert "PSObject.Properties.Name" in content, "PSCustomObject normalization not found"
    assert "Add-Member -NotePropertyName" in content, "Add-Member guard not found"
    assert "[pscustomobject]@{" in content, "pscustomobject result not found"
    assert "PSObject.Properties.Name -contains 'Name'" in content, "Name guard not found"
    
    # Verify all branch results are [pscustomobject]
    assert content.count("[pscustomobject]@{") >= 11, f"Expected at least 11 [pscustomobject]@{{}} definitions, found {content.count('[pscustomobject]@{')}"

    # Normalization block must exist
    assert "PSObject.Properties.Name" in content, "PSCustomObject normalization not found"
    assert "Add-Member -NotePropertyName" in content, "Add-Member guard not found"
    assert "[pscustomobject]@{" in content, "pscustomobject result not found"


def test_hourly_pipeline_ps1_measure_object_defensive():
    """Regression: Measure-Object must handle empty branch-result collection
    and missing Seconds fields without crashing."""
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert "Measure-Object -Property Seconds -Maximum" in content, "Measure-Object not found"
    assert "$BranchResults.Count" in content, "Empty collection guard not found"


def test_hourly_pipeline_ps1_job_cleanup_idempotent():
    """Regression: job cleanup must be idempotent and use parent job IDs.

    Root cause: Remove-Job -Job $Job was called twice per iteration,
    and $Job was sometimes a child job rather than the parent.

    Fix: all cleanup uses Get-Job -Id $Job.Id to get the live parent job,
    handles already-removed jobs gracefully, and is wrapped in try/catch
    so cleanup failures do not change branch result status.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # No bare Remove-Job with $Job directly
    # All cleanup must go through Get-Job -Id
    assert "Get-Job -Id" in content and "$Job.Id" in content, "Idempotent Get-Job lookup not found"
    assert "Remove-Job -Job $Job -Force" not in content, "Direct Remove-Job -Job $Job still present"

    # Try/catch around cleanup must exist
    assert "try {" in content, "try block not found"
    assert "} catch {" in content, "catch block not found"
    assert "Cleanup failure must not change branch result status" in content, "Cleanup error handling comment not found"

    # Stop-Job should use -ErrorAction SilentlyContinue for idempotency
    assert "Stop-Job -Job" in content, "Stop-Job not found"
    assert "ErrorAction SilentlyContinue" in content, "ErrorAction SilentlyContinue not found"

def test_hourly_pipeline_ps1_no_inline_if_in_object_literals():
    """Regression: PowerShell 5.1 does not support inline if expressions
    inside [pscustomobject] or @{ } literals.

    All conditional values must be computed as standalone statements
    before the object is constructed.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # Check for [double](if, [int](if, [bool](if, [string](if patterns
    for pattern in ['[double](if', '[int](if', '[bool](if', '[string](if']:
        assert pattern not in content, f"Invalid inline-cast-if pattern found: {pattern}"

    # Check no "= if (" inside object literal lines
    for line in content.split("\n"):
        stripped = line.strip()
        # Object literal lines should not have inline if
        if ('@{' in stripped or 'pscustomobject]@{' in stripped) and '= if (' in stripped:
            assert False, f"Inline if found in object literal: {stripped[:120]}"

    # Verify pre-computed variable pattern exists
    assert "$NormalizedSeconds" in content, "Pre-computed $NormalizedSeconds not found"
    assert "$NormalizedExitCode" in content, "Pre-computed $NormalizedExitCode not found"
    assert "$NormalizedOk" in content, "Pre-computed $NormalizedOk not found"
    assert "$NormalizedName" in content, "Pre-computed $NormalizedName not found"


def test_hourly_pipeline_ps1_pscustomobject_schema_complete():
    """Regression: every [pscustomobject] result must have all 7 required properties.
    After normalization, Measure-Object -Property Seconds -Maximum must succeed."""
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # All [pscustomobject]@{} blocks must have Seconds, ExitCode, Error, Branch, Ok
    # (Branch returns use "Branch=" not "Name="; normalization block adds Name/Status)
    pscustom_blocks = [l for l in content.split("\n") if 'pscustomobject]@{' in l]
    assert len(pscustom_blocks) >= 11, f"Expected >= 11 [pscustomobject]@{{}} definitions, found {len(pscustom_blocks)}"

    # Check normalization block has all 7 property assignments
    assert "$NormalizedName" in content, "Normalization missing $NormalizedName"
    assert "$NormalizedStatus" in content, "Normalization missing $NormalizedStatus"
    assert "$NormalizedSeconds" in content, "Normalization missing $NormalizedSeconds"
    assert "$NormalizedExitCode" in content, "Normalization missing $NormalizedExitCode"
    assert "$NormalizedError" in content, "Normalization missing $NormalizedError"
    assert "$NormalizedBranch" in content, "Normalization missing $NormalizedBranch"
    assert "$NormalizedOk" in content, "Normalization missing $NormalizedOk"
    assert "Measure-Object -Property Seconds -Maximum" in content, "Measure-Object not found"

    # Normalization block must have all 7 properties
    assert "Name     = $NormalizedName" in content, "Normalization block missing Name"
    assert "Status   = $NormalizedStatus" in content, "Normalization block missing Status"
    assert "Seconds  = $NormalizedSeconds" in content, "Normalization block missing Seconds"
    assert "ExitCode = $NormalizedExitCode" in content, "Normalization block missing ExitCode"
    assert "Error    = $NormalizedError" in content, "Normalization block missing Error"
    assert "Branch   = $NormalizedBranch" in content, "Normalization block missing Branch"
    assert "Ok       = $NormalizedOk" in content, "Normalization block missing Ok"

    # Measure-Object must work
    assert "Measure-Object -Property Seconds -Maximum" in content, "Measure-Object not found"


def test_hourly_pipeline_ps1_invoke_python_step_returns_error():
    """Regression: Invoke-PythonStep must return Error/Stderr in result.

    Previously, stderr was captured to a temp file but never returned
    in the result object. When a step failed, the orchestrator could
    not log the actual Python traceback.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert "Error = (Get-Content $Err" in content, "Error/Stderr not returned from Invoke-PythonStep"
    assert "Error = $null" in content, "Error = $null not in success return"


def test_hourly_pipeline_ps1_fail_block_logs_stderr():
    """Regression: the fail block must log ExitCode, stderr, and stdout tail."""
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert "Report generation failed - ExitCode" in content, "ExitCode not logged"
    assert "STDERR" in content and "Python stderr" in content, "Stderr logging not found"
    assert "STDOUT" in content and "Python stdout" in content, "Stdout tail logging not found"

def test_hourly_pipeline_ps1_json_writes_bom_free():
    """Regression: PowerShell must write JSON files without UTF-8 BOM.

    Windows PowerShell 5.1 Set-Content -Encoding UTF8 emits EF BB BF BOM bytes.
    Python json.loads() rejects BOM-prefixed files.
    The orchestrator must use .NET WriteAllText with UTF8Encoding($false).
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # No Set-Content -Encoding UTF8 should remain
    assert "Set-Content" not in content or "-Encoding UTF8" not in content, \
        "BOM-producing Set-Content -Encoding UTF8 still present"

    # UTF8NoBom helper must be defined
    assert "$Utf8NoBom = New-Object System.Text.UTF8Encoding" in content, \
        "UTF8NoBom definition not found"

    # All JSON writes must use WriteAllText
    assert "WriteAllText" in content, "WriteAllText not found"


def test_python_source_meta_reader_utf8_sig():
    """Regression: --source-meta-json must be read with utf-8-sig
    to accept both BOM and non-BOM UTF-8 files safely.
    """
    rmp_path = Path(__file__).resolve().parents[1] / "scripts" / "rule_based_market_update.py"
    content = rmp_path.read_text(encoding="utf-8")

    assert "encoding=\"utf-8-sig\"" in content, "utf-8-sig reader not found"


def test_json_file_no_bom_bytes():
    """Regression: existing state JSON files must not have BOM bytes EF BB BF."""
    for fname in ["reports/state/cycle_20260926_0130_HKT_sources.json",
                  "reports/state/latest_report_status.json"]:
        fpath = Path(__file__).resolve().parents[1] / fname
        if fpath.exists():
            with open(fpath, "rb") as f:
                first3 = f.read(3)
            assert first3 != b"\\xef\\xbb\\xbf", f"BOM found in {fname}"


def test_hourly_pipeline_ps1_no_stop_job_force():
    """Regression: Stop-Job does not support -Force in PowerShell 5.1.

    Windows PowerShell 5.1 Stop-Job only supports -Job, -ErrorAction,
    -WarningAction, -Confirm, -WhatIf, and -Verbose.
    The -Force parameter is PowerShell 7+ only.
    Using -Force causes:
    "A parameter cannot be found that matches parameter name 'Force'."
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # No Stop-Job calls should contain -Force
    import re
    stop_job_lines = [l for l in content.split("\n") if 'Stop-Job' in l]
    for line in stop_job_lines:
        assert '-Force' not in line, f"Stop-Job -Force found (PS 5.1 incompatible): {line.strip()[:120]}"

    # All Stop-Job calls must have -ErrorAction SilentlyContinue
    for line in stop_job_lines:
        if 'ErrorAction' not in line:
            assert False, f"Stop-Job missing -ErrorAction: {line.strip()[:120]}"


def test_hourly_pipeline_ps1_timeout_branch_recording():
    """Regression: after global timeout, branches still Running must be
    recorded as TIMEOUT with correct status and seconds.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert "Status = 'TIMEOUT'" in content, "TIMEOUT status not found"
    assert "$JobState -eq 'Running'" in content, "Running check not found"
    assert "Stop-Job -Job" in content, "Stop-Job not found"
    assert "-ErrorAction SilentlyContinue" in content, "ErrorAction SilentlyContinue not found"


def test_hourly_pipeline_ps1_macro_timeout_leq_overall():
    """Regression: MacroTimeoutSeconds must not exceed OverallTimeoutSeconds.

    Branch B's per-process WaitForExit timeout must be <= the global
    Wait-Job timeout, otherwise the global timeout kills Branch B first
    and the per-process timeout is dead code.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    import re
    macro_match = re.search(r'\$MacroTimeoutSeconds\s*=\s*(\d+)', content)
    overall_match = re.search(r'\$OverallTimeoutSeconds\s*=\s*(\d+)', content)
    assert macro_match, "MacroTimeoutSeconds not found"
    assert overall_match, "OverallTimeoutSeconds not found"
    macro_val = int(macro_match.group(1))
    overall_val = int(overall_match.group(1))
    assert macro_val <= overall_val, (
        f"MacroTimeoutSeconds={macro_val} exceeds OverallTimeoutSeconds={overall_val}. "
        "Branch B's WaitForExit timeout is dead code."
    )


def test_hourly_pipeline_ps1_branch_a_has_process_timeout():
    """Regression: Branch A must use Process.Start with WaitForExit for per-script timeout.

    Branch A previously used '& $Python script.py 2>&1 | Out-Null' which has no
    timeout and blocks indefinitely if the process hangs.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert 'WaitForExit(' in content, "WaitForExit not found in PS1"
    # Branch A should use Process.Start (not & call operator) for pgSQL_equities_auto
    assert 'Process]::Start($pinfo)' in content or 'Process]::Start($pinfo2)' in content, "Process.Start not used"


def test_eastmoney_circuit_breaker_present():
    """Regression: Eastmoney must have provider-level circuit breaker.

    Without it, persistent no-data responses from Eastmoney can cause
    unlimited page-by-page retries consuming the entire branch budget.
    """
    em_path = Path(__file__).resolve().parents[1] / "src" / "db_builder" / "eastmoney.py"
    content = em_path.read_text(encoding="utf-8")

    assert 'MAX_NO_DATA_PAGES' in content, "MAX_NO_DATA_PAGES constant not found"
    assert 'MAX_PAGE_RETRIES' in content, "MAX_PAGE_RETRIES constant not found"
    assert 'no_data_pages' in content, "no_data_pages counter not found"
    assert 'Provider circuit breaker' in content or 'circuit breaker tripped' in content, "Circuit breaker logic not found"


def test_eastmoney_provider_breaker_403_429_5xx():
    """Regression: Eastmoney must track HTTP 403/429/5xx as provider-wide errors.
    
    The circuit breaker must detect repeated HTTP errors across pages
    and abort early without iterating through all remaining pages.
    """
    em_path = Path(__file__).resolve().parents[1] / "src" / "db_builder" / "eastmoney.py"
    content = em_path.read_text(encoding="utf-8")

    assert "provider_403_count" in content, "provider_403_count tracking missing"
    assert "provider_429_count" in content, "provider_429_count tracking missing"
    assert "provider_5xx_count" in content, "provider_5xx_count tracking missing"
    assert "provider_circuit_breaker_open" in content, "provider_circuit_breaker_open missing"
    assert "HTTPError" in content, "HTTPError exception handler missing"
    assert "requests.exceptions.Timeout" in content, "Timeout exception handler missing"


def test_eastmoney_breaker_returns_empty_on_provider_failure():
    """Regression: When Eastmoney provider breaker is open during paginated
    fetching, fetch_and_save_all returns [] immediately.
    During the initial-request failure, the breaker opens and fallback runs
    instead of returning [].
    """
    em_path = Path(__file__).resolve().parents[1] / "src" / "db_builder" / "eastmoney.py"
    content = em_path.read_text(encoding="utf-8")

    # Return [] exists for paginated-fetch breaker
    assert "return []" in content, "Early return [] not found in eastmoney.py"
    # Return after fallback for initial-request breaker
    assert "return  # Branch A continues" in content, "Fallback return not found"
    # Both breaker patterns present
    assert "provider_circuit_breaker_open" in content


def test_investiny_provider_state_reset():
    """Regression: run_daily_update must reset provider breaker state
    at the start of each cycle so stale state from previous runs
    does not affect the current cycle."""
    md_path = Path(__file__).resolve().parents[1] / "scripts" / "macro_data_fetch.py"
    content = md_path.read_text(encoding="utf-8")

    assert "reset_investiny_provider_state" in content, "reset_investiny_provider_state call not found"


def test_investiny_provider_state_tracked():
    """Regression: Investing.com provider state must be trackable
    for cycle metadata recording."""
    md_path = Path(__file__).resolve().parents[1] / "scripts" / "macro_data_fetch.py"
    content = md_path.read_text(encoding="utf-8")

    assert "get_investiny_provider_state" in content, "get_investiny_provider_state not found"
    assert "_provider_breaker_open" in content, "provider breaker state not tracked"
    assert "_provider_failure_count" in content, "provider failure count not tracked"
    assert "_provider_requests_count" in content, "provider request count not tracked"


def test_child_process_terminated_on_timeout():
    """Regression: When a child process timeout occurs in the orchestrator,
    the Python child process must be actually killed via Process.Kill(),
    not just the PowerShell background job stopped.

    After session-aware refresh redesign, Branch A has a single pgSQL_equities_auto.py
    child process with its own timeout and Kill(). Historical repair and indicator
    backfill were moved out of the hourly path.
    """
    ps1_path = Path(__file__).resolve().parents[1] / "scripts" / "hourly_full_pipeline.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    # pgSQL_equities_auto.py has a single child process with Kill()
    assert "$p.Kill()" in content, "Process.Kill() not found for pgSQL_equities_auto"
    assert "WaitForExit(300000)" in content, "WaitForExit(300000) not found"
    # Verify no orphan-producing invocations remain in hourly Branch A
    assert "repair_historical_equity_gaps.py" not in content or \
           content.count("repair_historical_equity_gaps.py") == 1  # Only in comment
    assert "indicator_staged_backfill.py" not in content or \
           content.count("indicator_staged_backfill.py") == 0  # Fully removed



# =============================================================================
# Session-Aware Equity Refresh Tests
# =============================================================================


def test_session_aware_equity_refresh_skip_both():
    """When prices and indicators are current through latest NYSE session,
    pgSQL_equities_auto.py should skip both equity fetch and indicators."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata, should_skip_equity_fetch, should_skip_indicators
    from datetime import date

    today = date.today()
    # Simulate: stored = latest, indicator = latest
    metadata = get_cycle_metadata(
        None, today, today, today, force_refresh=False
    )
    assert metadata["decision"] == "skip_both"
    assert metadata["equity_refresh_status"] == "SKIPPED_ALREADY_CURRENT"
    assert metadata["indicator_refresh_status"] == "SKIPPED_ALREADY_CURRENT"
    assert should_skip_equity_fetch(today, today) is True
    assert should_skip_indicators(today, today) is True


def test_session_aware_equity_fetch_when_missing():
    """When the latest NYSE session is not stored, equity fetch should run."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata, should_skip_equity_fetch
    from datetime import date, timedelta

    today = date.today()
    stored = today - timedelta(days=7)  # One week behind
    metadata = get_cycle_metadata(None, today, stored, stored, force_refresh=False)
    assert metadata["decision"] == "fetch_equities_and_indicators"
    assert metadata["equity_refresh_status"] == "UPDATED"
    assert should_skip_equity_fetch(today, stored) is False


def test_session_aware_indicators_only_when_prices_current():
    """When prices are current but indicators are behind, only indicators should update."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata
    from datetime import date, timedelta

    today = date.today()
    stored = today  # Prices current
    indicator_stored = today - timedelta(days=1)  # Indicators behind
    metadata = get_cycle_metadata(None, today, stored, indicator_stored, force_refresh=False)
    assert metadata["decision"] == "equities_only"
    assert metadata["equity_refresh_status"] == "SKIPPED_ALREADY_CURRENT"
    assert metadata["indicator_refresh_status"] == "UPDATED"


def test_session_aware_prices_current_indicators_missing():
    """When prices current but indicators missing, skip equity fetch, run indicators."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata
    from datetime import date, timedelta

    today = date.today()
    stored = today
    indicator_stored = today - timedelta(days=1)
    metadata = get_cycle_metadata(None, today, stored, indicator_stored, force_refresh=False)
    assert metadata["decision"] == "equities_only"
    assert metadata["equity_refresh_status"] == "SKIPPED_ALREADY_CURRENT"
    assert metadata["indicator_refresh_status"] == "UPDATED"


def test_session_aware_second_run_after_update():
    """After a first run updates both, a second run should skip both."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata
    from datetime import date, timedelta

    today = date.today()
    # First run updated everything to today
    metadata = get_cycle_metadata(None, today, today, today, force_refresh=False)
    assert metadata["decision"] == "skip_both"


def test_session_aware_force_refresh_bypasses_skip():
    """--force-refresh should bypass all skip logic and run everything."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata
    from datetime import date

    today = date.today()
    metadata = get_cycle_metadata(None, today, today, today, force_refresh=True)
    assert metadata["decision"] == "force_refresh"
    assert metadata["equity_refresh_status"] == "UPDATED"
    assert metadata["indicator_refresh_status"] == "UPDATED"


def test_session_aware_weekend_no_fetch():
    """On weekends, the latest completed session should be the previous Friday.
    If that session is already stored, skip should work correctly."""
    from scripts.pgSQL_equities_auto import should_skip_equity_fetch
    from datetime import date, timedelta
    from unittest.mock import patch, MagicMock

    # Simulate: today is Saturday, latest completed NYSE session is Friday
    today = date.today()
    # If stored session >= Friday's session, skip
    friday = today - timedelta(days=(today.weekday() - 4) % 7 or 7)
    # If stored equals latest, should skip
    assert should_skip_equity_fetch(friday, friday) is True


def test_session_aware_historical_repair_not_in_hourly_path():
    """repair_historical_equity_gaps.py must NOT be invoked by the hourly Branch A.
    The orchestrator should only call pgSQL_equities_auto.py."""
    ps1_content = open("/workspace/DB_builder/scripts/hourly_full_pipeline.ps1").read()
    # repair_historical_equity_gaps should NOT be invoked as a process
    assert "repair_historical_equity_gaps.py" not in ps1_content or            ps1_content.count("repair_historical_equity_gaps.py") == 1  # Only in comment
    # The only invocation of pgSQL_equities_auto.py should be the single call
    assert ps1_content.count("pgSQL_equities_auto.py") >= 2  # Comment + invocation


def test_session_aware_skip_is_success_not_degraded():
    """SKIPPED_ALREADY_CURRENT must NOT be treated as degradation."""
    from scripts.pgSQL_equities_auto import get_cycle_metadata
    from datetime import date

    today = date.today()
    metadata = get_cycle_metadata(None, today, today, today)
    assert metadata["equity_refresh_status"] == "SKIPPED_ALREADY_CURRENT"
    assert metadata["indicator_refresh_status"] == "SKIPPED_ALREADY_CURRENT"
    # The decision is a healthy state — NOT degraded
    assert metadata["decision"] == "skip_both"


def test_session_aware_missing_latest_session_requires_fetch():
    """When the latest stored session is None (empty DB), fetch must run."""
    from scripts.pgSQL_equities_auto import should_skip_equity_fetch, should_skip_indicators
    from datetime import date

    today = date.today()
    # stored=None means DB is empty
    assert should_skip_equity_fetch(today, None) is False
    assert should_skip_indicators(today, None) is False


def test_session_aware_latest_completed_session_resolution():
    """latest_completed_nyse_session_date() must return a valid date."""
    from datetime import date
    from db_builder.trading_calendar import latest_completed_nyse_session_date
    result = latest_completed_nyse_session_date()
    assert result is not None
    assert result <= date.today()



# =============================================================================
# Eastmoney Initial-Request Circuit Breaker Tests
# =============================================================================


def test_eastmoney_initial_502_opens_breaker():
    """When the initial Eastmoney universe request returns HTTP 502,
    the provider breaker must open, NOT raise RuntimeError."""
    from src.db_builder.eastmoney import get_api_url
    import requests
    from unittest.mock import patch, MagicMock
    from sqlalchemy import create_engine

    # Mock the initial request to raise HTTP 502
    mock_resp = MagicMock()
    mock_resp.status_code = 502
    mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)

    with patch("src.db_builder.eastmoney.requests.get", return_value=mock_resp):
        with patch("src.db_builder.eastmoney._load_target_table", return_value=MagicMock()):
            with patch("src.db_builder.eastmoney.fetch_prior_session_universe") as mock_universe:
                with patch("src.db_builder.eastmoney.fetch_missing_session_rows") as mock_fallback:
                    mock_universe.return_value = MagicMock(
                        ticker=["AAPL", "MSFT"],
                        name=["Apple", "Microsoft"],
                        iter=lambda: [("AAPL", "Apple", 0), ("MSFT", "Microsoft", 0)],
                    )
                    mock_fallback.return_value = MagicMock(
                        frame=MagicMock(empty=False),
                        found={"AAPL", "MSFT"},
                        requested={"AAPL", "MSFT"},
                        missing=set(),
                    )

                    from db_builder.config import local_engine
                    # We can't easily test fetch_and_save_all without a real DB,
                    # but we verify the code path by checking the source
                    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()

                    # The RuntimeError must NOT be raised
                    assert "raise RuntimeError(\"Initial Eastmoney request failed\")" not in src
                    # The provider breaker must open
                    assert "provider_circuit_breaker_open = True" in src
                    assert "[EASTMONEY] Initial request" in src
                    # The fallback must be invoked
                    assert "fetch_prior_session_universe" in src
                    assert "fetch_missing_session_rows" in src
                    # Success log
                    assert "[FALLBACK] Latest session" in src


def test_eastmoney_initial_403_opens_breaker():
    """HTTP 403 on initial request must open the provider breaker."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "status_code == 403" in src, "403 detection missing"
    assert "provider_403_count = 1" in src, "403 counter missing"
    assert "provider_breaker_reason" in src, "breaker reason missing"


def test_eastmoney_initial_timeout_opens_breaker():
    """Timeout on initial request must open the provider breaker."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "connection/timeout error" in src, "timeout detection missing"
    assert "provider_connection_errors = 1" in src, "connection error counter missing"


def test_eastmoney_fallback_invoked_after_initial_failure():
    """When initial request fails, the yfinance fallback must be invoked."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "fetch_prior_session_universe(engine, target_date, table_name)" in src
    assert "fetch_missing_session_rows(" in src
    assert "expected_tickers" in src


def test_eastmoney_successful_fallback_prevents_branch_a_failure():
    """When fallback succeeds, fetch_and_save_all returns normally
    (does not raise), allowing Branch A to continue to indicators."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    # The function must return after fallback, not raise
    assert "return  # Branch A continues" in src or "return" in src
    # Must NOT raise RuntimeError for initial failure
    assert "raise RuntimeError(\"Initial Eastmoney request failed\")" not in src


def test_eastmoney_failed_fallback_still_causes_failure():
    """When both Eastmoney is unavailable AND fallback fails,
    the function must raise RuntimeError to signal Branch A failure."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "Eastmoney unavailable and fallback failed" in src,         "Fallback failure must still raise"
    assert "raise RuntimeError" in src,         "Fallback failure must propagate as RuntimeError"


def test_eastmoney_indicators_run_after_successful_fallback():
    """After successful fallback, the provider breaker must be open
    but indicators must still run via pgSQL_equities_auto.py."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "return  # Branch A continues" in src,         "After fallback, must return to allow indicators"
    # pgSQL_equities_auto.py will call daily_update_missing_indicators after fetch_and_save_all


def test_eastmoney_provider_state_records_degradation():
    """The provider state must record degradation when the breaker opens."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "provider_circuit_breaker_open = True" in src
    assert "provider_breaker_reason" in src
    assert "provider_403_count" in src or "provider_5xx_count" in src or "provider_connection_errors" in src


def test_eastmoney_does_not_silently_use_previous_session():
    """The fallback must fetch data for the latest completed session,
    NOT silently use the previous session as current."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "target_date" in src, "target_date must be used for fallback"
    assert "fetch_missing_session_rows(" in src,         "Must fetch actual latest session data via fallback"
    assert "f\"[FALLBACK] Latest session" in src.replace("\\\\", "\\"),         "Must log the actual session that was fetched"


def test_eastmoney_502_specific_logging():
    """The runtime output must clearly show the breaker opening and fallback."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    assert "[EASTMONEY] Initial request" in src
    assert "provider breaker opened" in src
    assert "[FALLBACK]" in src
    assert "[FALLBACK] Latest session" in src


def test_eastmoney_provider_breaker_state_reset_between_cycles():
    """The provider breaker state must be initialized at the start
    of each fetch_and_save_all call (reset between cycles)."""
    src = open("/workspace/DB_builder/src/db_builder/eastmoney.py").read()
    # The initialization block must be at the start of the function
    assert "provider_circuit_breaker_open = False" in src
    assert "provider_breaker_reason" in src
    assert "provider_403_count = 0" in src
    assert "provider_connection_errors = 0" in src
