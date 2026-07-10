from datetime import date
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.repair_historical_equity_gaps import earliest_repair_dates


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
