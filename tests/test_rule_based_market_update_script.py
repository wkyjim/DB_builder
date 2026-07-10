from __future__ import annotations

import sys
from pathlib import Path

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
