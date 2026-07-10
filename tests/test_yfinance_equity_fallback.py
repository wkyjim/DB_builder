from __future__ import annotations

from datetime import date

import pandas as pd

from db_builder.yfinance_equity_fallback import (
    fetch_missing_range_rows,
    _target_row,
    yfinance_symbol,
)


def test_yfinance_symbol_maps_classes_units_and_preferreds():
    assert yfinance_symbol("BRK_B", "Berkshire Hathaway Class B") == "BRK-B"
    assert yfinance_symbol("AIIA_U", "AI Infrastructure Acquisition Unit") == "AIIA-UN"
    assert yfinance_symbol("ABR_D", "Arbor Realty Trust Series D Preferred") == "ABR-PD"


def test_target_row_uses_exact_requested_date():
    columns = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low", "Close", "Volume"]])
    batch = pd.DataFrame(
        [
            [9.5, 10.2, 9.4, 10.0, 100],
            [10.1, 11.2, 10.0, 11.0, 200],
        ],
        index=pd.to_datetime(["2026-07-07", "2026-07-08"]),
        columns=columns,
    )
    batch.index.name = "Date"

    result = _target_row(
        batch,
        db_ticker="AAA",
        yf_ticker="AAA",
        target_date="2026-07-08",
        metadata={"name": "AAA Inc", "market": "105", "close": 10.0},
    )

    assert result["date"].isoformat() == "2026-07-08"
    assert result["ticker"] == "AAA"
    assert result["prev_close"] == 10.0
    assert result["close"] == 11.0


def test_range_fallback_requests_each_batch_once(monkeypatch):
    dates = [date(2026, 5, 13), date(2026, 5, 19)]
    columns = pd.MultiIndex.from_product([["AAA"], ["Open", "High", "Low", "Close", "Volume"]])
    batch = pd.DataFrame(
        [
            [9.0, 10.0, 8.0, 9.5, 100.0],
            [10.0, 11.0, 9.0, 10.5, 120.0],
        ],
        index=pd.to_datetime(dates),
        columns=columns,
    )
    batch.index.name = "Date"
    calls = []

    def fake_download(symbols, start_date, end_date, **kwargs):
        calls.append((symbols, start_date, end_date))
        return batch

    monkeypatch.setattr(
        "db_builder.yfinance_equity_fallback._download_batch_range",
        fake_download,
    )
    universe = pd.DataFrame(
        [{"ticker": "AAA", "name": "AAA", "market": "105", "close": 9.0}]
    )
    result = fetch_missing_range_rows(
        universe,
        {dates[0]: {"AAA"}, dates[1]: {"AAA"}},
    )

    assert len(calls) == 1
    assert len(result.frame) == 2
    assert result.missing_keys == ()
