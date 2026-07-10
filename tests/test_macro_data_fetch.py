from datetime import date, datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import macro_data_fetch


def sample_bars():
    return pd.DataFrame(
        [
            {
                "Date": pd.Timestamp("2026-07-07"),
                "Open": 100.0,
                "High": 102.0,
                "Low": 99.0,
                "Close": 101.0,
                "Adj Close": 101.0,
                "Volume": 1000,
            },
            {
                "Date": pd.Timestamp("2026-07-08"),
                "Open": 101.0,
                "High": 104.0,
                "Low": 100.0,
                "Close": 103.0,
                "Adj Close": 103.0,
                "Volume": 700,
            },
        ]
    )


def test_extract_latest_unfinished_row_builds_live_snapshot(monkeypatch):
    monkeypatch.setattr(
        macro_data_fetch,
        "symbol_date_has_closed",
        lambda symbol, bar_date: False,
    )
    observed_at = datetime(2026, 7, 8, 15, 0, tzinfo=ZoneInfo("UTC"))

    row = macro_data_fetch.extract_latest_unfinished_row(
        sample_bars(),
        "^GSPC",
        "S&P 500",
        "stock_index",
        observed_at=observed_at,
    )

    assert row["symbol"] == "^GSPC"
    assert row["market_date"] == date(2026, 7, 8)
    assert row["observed_at"] == observed_at
    assert row["prev_close"] == 101.0
    assert row["pct_chg"] == 1.9802
    assert row["is_market_closed"] is False


def test_extract_latest_unfinished_row_skips_completed_bar(monkeypatch):
    monkeypatch.setattr(
        macro_data_fetch,
        "symbol_date_has_closed",
        lambda symbol, bar_date: True,
    )

    row = macro_data_fetch.extract_latest_unfinished_row(
        sample_bars(),
        "^GSPC",
        "S&P 500",
        "stock_index",
    )

    assert row is None
