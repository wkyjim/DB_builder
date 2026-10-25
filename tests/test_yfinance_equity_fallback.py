from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import db_builder.yfinance_equity_fallback as yf_module
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


def test_prior_session_universe_keeps_etfs_and_warrants():
    """The fallback universe must mirror the primary path's coverage.

    fetch_prior_session_universe used to run filter_core_coverage(), which drops
    ETFs, warrants, rights, preferreds and funds. When Eastmoney is down the
    fallback is the only writer, so every one of those silently stopped
    updating: the 2026-10-23 session landed with 19 of 1,355 ETFs, and 2,652
    of the 2,707 missing tickers were coverage_eligible = true.
    """
    import pandas as pd

    frame = pd.DataFrame(
        [
            {"ticker": "AAPL", "name": "Apple Inc", "close": 200.0},
            {"ticker": "SPY", "name": "SPDR S&P 500 ETF", "close": 500.0},
            {"ticker": "AAAU", "name": "Goldman Sachs Physical Gold ETF", "close": 20.0},
            {"ticker": "NSTR_WS", "name": "Newstar Rights", "close": 1.5},
            {"ticker": "ABR_D", "name": "Arbor Realty Trust 6.375% Redeemable", "close": 25.0},
        ]
    )

    captured: dict = {}

    def _fake_read_sql(query, engine, params=None, **kwargs):
        captured["query"] = str(query)
        captured["params"] = params
        return frame.copy()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(yf_module.pd, "read_sql", _fake_read_sql)

    out = yf_module.fetch_prior_session_universe(
        None, "2026-10-23", yf_module.RAW_TABLE
    )
    monkeypatch.undo()

    assert len(out) == 5, "every prior-session row must survive"
    assert set(out["ticker"]) == {"AAPL", "SPY", "AAAU", "NSTR_WS", "ABR_D"}
