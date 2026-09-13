from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.rule_based_market_update import publish_to_dashboard


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
