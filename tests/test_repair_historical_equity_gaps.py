from datetime import date
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.repair_historical_equity_gaps import (
    discover_repair_targets,
    earliest_repair_dates,
)


def test_earliest_repair_dates_are_per_ticker():
    frame = pd.DataFrame(
        [
            {"date": date(2026, 5, 13), "ticker": "AAA"},
            {"date": date(2026, 6, 17), "ticker": "AAA"},
            {"date": date(2026, 6, 12), "ticker": "BBB"},
        ]
    )

    assert earliest_repair_dates(frame) == {
        "AAA": date(2026, 5, 13),
        "BBB": date(2026, 6, 12),
    }


def test_discovery_includes_fully_missing_sessions(monkeypatch):
    monkeypatch.setattr(
        "scripts.repair_historical_equity_gaps.expected_sessions",
        lambda start, end: [date(2026, 8, 13), date(2026, 8, 14)],
    )
    monkeypatch.setattr(
        "scripts.repair_historical_equity_gaps.fetch_coverage_eligible_tickers",
        lambda engine: ["AAA"],
    )
    monkeypatch.setattr(
        "scripts.repair_historical_equity_gaps.fetch_raw_coverage_keys",
        lambda *args, **kwargs: pd.DataFrame(
            [{"date": date(2026, 8, 13), "ticker": "AAA"}]
        ),
    )

    targets = discover_repair_targets(None, date(2026, 8, 14), date(2026, 8, 14), 0.98)

    assert targets == {date(2026, 8, 14): {"AAA"}}
