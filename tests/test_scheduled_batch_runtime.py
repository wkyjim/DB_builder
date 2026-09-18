"""Safe Windows runtime checks for the scheduled database batch wrappers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
PROJECT_PYTHON = Path(r"C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe")
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class BatchSpec:
    filename: str
    mutex_name: str
    scripts: tuple[str, ...]
    log_glob: str


BATCHES = (
    BatchSpec(
        filename="auto_postgreSQL_db.bat",
        mutex_name=r"Local\DBBuilderAutoPostgreSqlDb",
        scripts=(
            "health_check.py",
            "pgSQL_equities_auto.py",
            "repair_historical_equity_gaps.py",
            "pgSQL_daily_bulk_sync_to_neon.py",
            "indicator_staged_backfill.py",
            "freshness_check.py",
        ),
        log_glob="auto_postgreSQL_db_*.log",
    ),
    BatchSpec(
        filename="auto_macro_db.bat",
        mutex_name=r"Local\DBBuilderAutoMacroDb",
        scripts=(
            "health_check.py",
            "security_classification_fetch.py",
            "macro_data_fetch.py",
            "finra_short_incremental.py",
            "short_analytics_latest_sync.py",
            "economic_data_fetch.py",
            "global_economic_data_fetch.py",
            "rule_based_market_update.py",
            "freshness_check.py",
        ),
        log_glob="auto_macro_db_*.log",
    ),
)


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not PROJECT_PYTHON.exists(),
    reason="scheduled batch runtime checks require the configured Windows runtime",
)


STUB_SCRIPT = """\
from pathlib import Path
import os
import sys

name = Path(__file__).name
with (Path.cwd() / "invocations.log").open("a", encoding="utf-8") as stream:
    stream.write(name + "\\n")

if name == "freshness_check.py":
    raise SystemExit(int(os.environ.get("DB_BUILDER_TEST_FRESHNESS_EXIT", "0")))
if name == os.environ.get("DB_BUILDER_TEST_FAIL_SCRIPT"):
    raise SystemExit(1)
raise SystemExit(0)
"""


def prepare_sandbox(tmp_path: Path, spec: BatchSpec) -> Path:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    batch_path = scripts_dir / spec.filename
    source = (ROOT / "scripts" / spec.filename).read_text(encoding="utf-8")
    # Failure-path tests exercise all retries without waiting two minutes.
    source = source.replace("Start-Sleep -Seconds 60", "Start-Sleep -Milliseconds 1")
    batch_path.write_text(source, encoding="utf-8", newline="\r\n")

    for script_name in spec.scripts:
        (scripts_dir / script_name).write_text(STUB_SCRIPT, encoding="utf-8")
    (scripts_dir / "telegram_system_alert.bat").write_text(
        "@echo off\r\nexit /b 0\r\n", encoding="utf-8", newline=""
    )
    return batch_path


def run_batch(batch_path: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["cmd.exe", "/d", "/c", str(batch_path)],
        cwd=batch_path.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def latest_log(tmp_path: Path, spec: BatchSpec) -> str:
    logs = sorted((tmp_path / "logs").glob(spec.log_glob))
    assert logs, "batch did not create its first workflow log"
    return logs[-1].read_text(encoding="utf-8")


def can_acquire_mutex(mutex_name: str) -> bool:
    command = (
        f"$m = [System.Threading.Mutex]::new($false, '{mutex_name}'); "
        "$acquired = $m.WaitOne(0); "
        "if ($acquired) { $m.ReleaseMutex() }; $m.Dispose(); "
        "if (-not $acquired) { exit 1 }"
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    return result.returncode == 0


@pytest.mark.parametrize("spec", BATCHES, ids=lambda spec: spec.filename)
def test_success_path_runs_freshness_and_creates_log(tmp_path: Path, spec: BatchSpec):
    batch_path = prepare_sandbox(tmp_path, spec)

    result = run_batch(batch_path)

    assert result.returncode == 0, result.stdout + result.stderr
    log = latest_log(tmp_path, spec)
    assert "[START]" in log
    assert "[FRESHNESS] Running post-ingestion freshness validation" in log
    assert "[FRESHNESS OK]" in log
    invocations = (tmp_path / "invocations.log").read_text(encoding="utf-8").splitlines()
    assert invocations[-1] == "freshness_check.py"


@pytest.mark.parametrize("spec", BATCHES, ids=lambda spec: spec.filename)
def test_child_failure_propagates_and_mutex_is_cleaned_up(tmp_path: Path, spec: BatchSpec):
    batch_path = prepare_sandbox(tmp_path, spec)

    result = run_batch(batch_path, DB_BUILDER_TEST_FAIL_SCRIPT="health_check.py")

    assert result.returncode == 1, result.stdout + result.stderr
    assert can_acquire_mutex(spec.mutex_name)
    assert "failed after 3 retries" in latest_log(tmp_path, spec)


@pytest.mark.parametrize("spec", BATCHES, ids=lambda spec: spec.filename)
def test_freshness_failure_exit_code_propagates(tmp_path: Path, spec: BatchSpec):
    batch_path = prepare_sandbox(tmp_path, spec)

    result = run_batch(batch_path, DB_BUILDER_TEST_FRESHNESS_EXIT="2")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "[FRESHNESS FAIL]" in latest_log(tmp_path, spec)


@pytest.mark.parametrize("spec", BATCHES, ids=lambda spec: spec.filename)
def test_concurrent_invocation_skips_without_running_children(tmp_path: Path, spec: BatchSpec):
    batch_path = prepare_sandbox(tmp_path, spec)
    ready_path = tmp_path / "mutex-ready"
    holder_command = (
        f"$m = [System.Threading.Mutex]::new($false, '{spec.mutex_name}'); "
        "$acquired = $m.WaitOne(0); if (-not $acquired) { exit 3 }; "
        f"Set-Content -LiteralPath '{ready_path}' -Value ready; "
        "try { Start-Sleep -Seconds 20 } finally { $m.ReleaseMutex(); $m.Dispose() }"
    )
    holder = subprocess.Popen(
        [str(POWERSHELL), "-NoProfile", "-Command", holder_command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_path.exists(), "mutex holder did not become ready"

        result = run_batch(batch_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "[SKIP]" in latest_log(tmp_path, spec)
        assert not (tmp_path / "invocations.log").exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)
