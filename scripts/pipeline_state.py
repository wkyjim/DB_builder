"""Pipeline cycle state management for the hourly full-refresh pipeline.

Writes machine-readable cycle state to reports/state/latest_report_status.json
using atomic temp-write + rename. The downstream Market Intelligence HTML
gate (readiness_gate.py) consumes this file; it never relies on file mtimes.

Status model (report readiness is SEPARATE from publication readiness):
  overall_status:
    RUNNING          cycle in progress
    REPORT_READY     full report generated + validated locally
    FAILED           critical source failure or report generation failed
  publication_status:
    NOT_PUBLISHED    report not yet committed
    PUSHED           commit + push succeeded, HEAD == origin/main
    PUSH_FAILED      commit ok, push failed (report still analytically usable)
    COMMIT_FAILED    commit itself failed

Per-source severity classes:
  CRITICAL   report cannot be generated without it (equities, rates,
             FX/commodities, macro-market core)
  IMPORTANT  report degrades meaningfully (economic releases, ETF flows,
             FINRA short, news signals)
  OPTIONAL   cosmetic/auxiliary contribution (news classification overlay,
             supplementary indices)
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "reports" / "state"
STATE_PATH = STATE_DIR / "latest_report_status.json"

SOURCE_SEVERITY = {
    "us_equities": "CRITICAL",
    "equity_indicators": "CRITICAL",
    "macro_rates": "CRITICAL",
    "macro_fx_commodities": "CRITICAL",
    "macro_market_indices": "CRITICAL",
    "economic_releases": "IMPORTANT",
    "etf_flows": "IMPORTANT",
    "finra_short": "IMPORTANT",
    "news_fetch": "IMPORTANT",
    "news_classify": "OPTIONAL",
    "news_signals": "IMPORTANT",
    "asia_futures": "OPTIONAL",
}

CRITICAL_SOURCES = {k for k, v in SOURCE_SEVERITY.items() if v == "CRITICAL"}


def now_hkt() -> datetime:
    return datetime.now(HKT)


def iso_hkt(dt: datetime | None = None) -> str:
    dt = dt or now_hkt()
    return dt.isoformat(timespec="seconds")


def make_cycle_id(dt: datetime | None = None) -> str:
    dt = dt or now_hkt()
    return dt.strftime("%Y%m%d_%H%M") + "_HKT"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def new_cycle_state(cycle_id: str | None = None) -> dict:
    started = now_hkt()
    return {
        "cycle_id": cycle_id or make_cycle_id(started),
        "started_at": iso_hkt(started),
        "completed_at": None,
        "overall_status": "RUNNING",
        "publication_status": "NOT_PUBLISHED",
        "consecutive_failures": 0,
        "sources": {},
        "degraded_sources": [],
        "latest_report": {
            "generated_at": None,
            "path": None,
            "sha256": None,
            "cycle_id": None,
        },
        "git": {
            "commit": None,
            "pushed": False,
            "head_equals_origin": None,
            "error": None,
        },
    }


def record_source(
    state: dict,
    name: str,
    *,
    status: str,
    observation_date=None,
    retrieved_at=None,
    note: str | None = None,
) -> None:
    """Record per-source status.

    status: OK | DEGRADED | FAILED | SKIPPED
    observation_date: the date/time the data itself is current through.
    retrieved_at: when we checked/fetched it this cycle (HKT ISO).
    """
    entry = {
        "status": status,
        "severity": SOURCE_SEVERITY.get(name, "OPTIONAL"),
        "observation_date": str(observation_date) if observation_date else None,
        "retrieved_at": retrieved_at or iso_hkt(),
    }
    if note:
        entry["note"] = note
    state["sources"][name] = entry
    _refresh_degraded(state)


def _refresh_degraded(state: dict) -> None:
    degraded = []
    for name, entry in state["sources"].items():
        if entry["status"] in ("DEGRADED", "FAILED"):
            degraded.append(name)
    state["degraded_sources"] = sorted(degraded)


def critical_sources_ok(state: dict) -> bool:
    for name in CRITICAL_SOURCES:
        entry = state["sources"].get(name)
        if entry is None or entry["status"] != "OK":
            return False
    return True


def finalize_report_ready(state: dict, report_path: Path, generated_at: datetime | None = None) -> str:
    """Mark the locally generated report REPORT_READY (pre-publication)."""
    state["latest_report"] = {
        "generated_at": iso_hkt(generated_at or now_hkt()),
        "path": str(report_path),
        "sha256": sha256_file(report_path),
        "cycle_id": state["cycle_id"],
    }
    if critical_sources_ok(state):
        state["overall_status"] = "REPORT_READY"
    else:
        state["overall_status"] = "FAILED"
    return state["overall_status"]


def finalize_failed(state: dict, reason: str) -> None:
    state["overall_status"] = "FAILED"
    state["failure_reason"] = reason
    state["completed_at"] = iso_hkt()


def record_publication(
    state: dict,
    *,
    commit: str | None,
    pushed: bool,
    head_equals_origin: bool | None,
    error: str | None = None,
) -> None:
    state["git"] = {
        "commit": commit,
        "pushed": pushed,
        "head_equals_origin": head_equals_origin,
        "error": error,
    }
    if commit is None:
        state["publication_status"] = "COMMIT_FAILED"
    elif pushed and head_equals_origin:
        state["publication_status"] = "PUSHED"
    else:
        state["publication_status"] = "PUSH_FAILED"
    state["completed_at"] = iso_hkt()


def bump_consecutive_failures(prev_state: dict | None, state: dict) -> None:
    if state["overall_status"] == "FAILED":
        prior = 0
        if prev_state and prev_state.get("overall_status") == "FAILED":
            prior = int(prev_state.get("consecutive_failures", 0))
        state["consecutive_failures"] = prior + 1
    else:
        state["consecutive_failures"] = 0


def load_state(path: Path | None = None) -> dict | None:
    path = path or STATE_PATH
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state: dict, path: Path | None = None) -> Path:
    """Atomically write state JSON: temp file in same dir + os.replace."""
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".latest_report_status.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
