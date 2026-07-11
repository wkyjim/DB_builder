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


def test_macro_etf_flow_post_step_refreshes_local_flows_and_signals(monkeypatch):
    calls = []

    def fake_setup_flow_tables(engine):
        calls.append(("setup", engine))

    def fake_run_etf_flow_fetch(engine, *, tickers, dry_run, start_date):
        calls.append(("fetch", engine, tickers, dry_run, start_date))
        return {"rows": 3, "upserted": 3, "recomputed": 6, "sample": []}

    def fake_run_positioning_flow_signal_update(engine, *, dry_run):
        calls.append(("signals", engine, dry_run))
        return {"rows": 2, "upserted": 2}

    import db_builder.etf_flows as etf_flows
    import db_builder.positioning_flow_signals as positioning_flow_signals

    monkeypatch.setattr(macro_data_fetch, "local_engine", object())
    monkeypatch.setattr(positioning_flow_signals, "setup_flow_tables", fake_setup_flow_tables)
    monkeypatch.setattr(etf_flows, "run_etf_flow_fetch", fake_run_etf_flow_fetch)
    monkeypatch.setattr(positioning_flow_signals, "run_positioning_flow_signal_update", fake_run_positioning_flow_signal_update)

    result = macro_data_fetch.run_etf_flow_update_after_macro(
        start_date=date(2026, 1, 1),
        tickers=["spy", "ivv"],
        dry_run=False,
    )

    assert result["upserted"] == 3
    assert calls[0][0] == "setup"
    assert calls[1][0] == "fetch"
    assert calls[1][2] == ["SPY", "IVV"]
    assert calls[1][3] is False
    assert calls[1][4] == date(2026, 1, 1)
    assert calls[2][0] == "signals"


def test_macro_etf_flow_post_step_dry_run_does_not_refresh_signals(monkeypatch):
    calls = []

    def fake_setup_flow_tables(engine):
        calls.append(("setup", engine))

    def fake_run_etf_flow_fetch(engine, *, tickers, dry_run, start_date):
        calls.append(("fetch", tickers, dry_run, start_date))
        return {"rows": 1, "upserted": 0, "recomputed": 0, "sample": []}

    def fake_run_positioning_flow_signal_update(engine, *, dry_run):
        calls.append(("signals", dry_run))
        return {"rows": 0, "upserted": 0}

    import db_builder.etf_flows as etf_flows
    import db_builder.positioning_flow_signals as positioning_flow_signals

    monkeypatch.setattr(macro_data_fetch, "local_engine", object())
    monkeypatch.setattr(positioning_flow_signals, "setup_flow_tables", fake_setup_flow_tables)
    monkeypatch.setattr(etf_flows, "run_etf_flow_fetch", fake_run_etf_flow_fetch)
    monkeypatch.setattr(positioning_flow_signals, "run_positioning_flow_signal_update", fake_run_positioning_flow_signal_update)

    macro_data_fetch.run_etf_flow_update_after_macro(dry_run=True)

    assert calls == [("fetch", None, True, None)]
