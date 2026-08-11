from datetime import date, datetime
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import macro_data_fetch


def test_macro_fetch_registry_includes_dashboard_tape_symbols():
    symbols = {symbol for symbol, _, _ in macro_data_fetch.ASSETS}
    expected = {
        "^NDX",
        "^HSI",
        "NIY=F",
        "^KS200",
        "JPY=X",
        "EURUSD=X",
        "DX-Y.NYB",
        "^SKEW",
        "US2YT=X",
        "US3YT=X",
        "US5YT=X",
        "US7YT=X",
        "US10YT=X",
        "US20YT=X",
        "US30YT=X",
        "KOR200c1",
        "HK50",
        "CIHc1",
        "CHFUSD=X",
        "BZ=F",
        "CL=F",
    }

    assert expected <= symbols
    assert expected <= set(macro_data_fetch.SYMBOL_CLOSE_MAP)
    assert {
        "US2YT=X",
        "US3YT=X",
        "US5YT=X",
        "US7YT=X",
        "US10YT=X",
        "US20YT=X",
        "US30YT=X",
        "KOR200c1",
        "HK50",
        "CIHc1",
        "BZ=F",
        "CL=F",
    } <= set(macro_data_fetch.INVESTINY_ASSETS)
    assert macro_data_fetch.INVESTING_SOURCE_SYMBOLS == {
        "BZ=F": "LCOV6",
        "CL=F": "OIL",
    }


def test_chf_uses_yfinance_and_has_close_configuration():
    assert "CHFUSD=X" not in macro_data_fetch.INVESTINY_ASSETS
    assert macro_data_fetch.get_close_config("CHFUSD=X") == {
        "tz": "America/New_York",
        "close_time": "17:00",
    }


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


def test_fetch_investiny_symbol_df_normalizes_historical_payload(monkeypatch):
    def fake_historical_data(*, investing_id, from_date, to_date, interval):
        assert investing_id == macro_data_fetch.INVESTINY_ASSETS["US10YT=X"]
        assert interval == "D"
        return {
            "date": ["07/01/2026", "07/02/2026"],
            "open": [4.4, 4.5],
            "high": [4.6, 4.7],
            "low": [4.3, 4.4],
            "close": [4.55, 4.65],
        }

    fake_module = types.SimpleNamespace(historical_data=fake_historical_data)
    monkeypatch.setitem(sys.modules, "investiny", fake_module)
    monkeypatch.setattr(macro_data_fetch.time, "sleep", lambda *_args, **_kwargs: None)

    df, target_start = macro_data_fetch.fetch_investiny_symbol_df(
        "US10YT=X",
        start_date=date(2026, 7, 1),
    )

    assert target_start == date(2026, 7, 1)
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df.iloc[0]["Date"] == pd.Timestamp("2026-07-01")
    assert df.iloc[1]["Adj Close"] == 4.65
    assert pd.isna(df.iloc[0]["Volume"])


def test_fetch_investiny_symbol_df_drops_weekend_rows(monkeypatch):
    def fake_historical_data(*, investing_id, from_date, to_date, interval):
        return {
            "date": ["07/03/2026", "07/05/2026", "07/06/2026"],
            "open": [4.4, 4.5, 4.6],
            "high": [4.6, 4.7, 4.8],
            "low": [4.3, 4.4, 4.5],
            "close": [4.55, 4.65, 4.75],
        }

    fake_module = types.SimpleNamespace(historical_data=fake_historical_data)
    monkeypatch.setitem(sys.modules, "investiny", fake_module)
    monkeypatch.setattr(macro_data_fetch.time, "sleep", lambda *_args, **_kwargs: None)

    df, _ = macro_data_fetch.fetch_investiny_symbol_df(
        "US10YT=X",
        start_date=date(2026, 7, 1),
    )

    assert [item.date() for item in df["Date"]] == [date(2026, 7, 3), date(2026, 7, 6)]


def test_process_investiny_oil_live_row_uses_investing_source(monkeypatch):
    monkeypatch.setattr(
        macro_data_fetch,
        "fetch_investiny_symbol_df",
        lambda *_args, **_kwargs: (sample_bars(), date(2026, 7, 7)),
    )
    monkeypatch.setattr(
        macro_data_fetch,
        "extract_latest_unfinished_row",
        lambda **_kwargs: {"symbol": "BZ=F"},
    )
    monkeypatch.setattr(
        macro_data_fetch,
        "remove_unfinished_bars",
        lambda df, _symbol: df,
    )

    _rows, live_row = macro_data_fetch.process_investiny_symbol(
        "BZ=F",
        {"name": "Brent Crude Oil Future", "asset_type": "futures"},
    )

    assert live_row["source"] == "investing.com"


def test_upsert_new_macro_rows_checks_neon_independently(monkeypatch):
    local = object()
    neon = object()
    rows = [
        {
            "date": date(2026, 8, 10),
            "symbol": "CHFUSD=X",
            "name": "CHF/USD",
            "asset_type": "fx",
        }
    ]
    writes = []

    def fake_existing(engine, symbols, min_date, max_date):
        del symbols, min_date, max_date
        if engine is local:
            return pd.DataFrame([{"symbol": "CHFUSD=X", "date": date(2026, 8, 10)}])
        return pd.DataFrame(columns=["symbol", "date"])

    monkeypatch.setattr(macro_data_fetch, "local_engine", local)
    monkeypatch.setattr(macro_data_fetch, "neon_engine", neon)
    monkeypatch.setattr(macro_data_fetch, "fetch_existing_symbol_dates", fake_existing)
    monkeypatch.setattr(
        macro_data_fetch,
        "upsert_macro",
        lambda engine, new_rows: writes.append((engine, new_rows)),
    )

    inserted = macro_data_fetch.upsert_new_macro_rows(
        rows,
        ["CHFUSD=X"],
        upsert_neon=True,
        provider_label="YF",
    )

    assert inserted == 1
    assert writes == [(neon, rows)]


def test_macro_etf_flow_post_step_refreshes_local_flows_and_signals(monkeypatch):
    calls = []

    def fake_setup_flow_tables(engine):
        calls.append(("setup", engine))

    def fake_plan_etf_flow_missing_fetch(engine, *, tickers, start_date):
        calls.append(("plan", engine, tickers, start_date))
        return {
            "target_date": date(2026, 7, 17),
            "start_date": start_date,
            "tickers": tickers,
            "latest_dates": {"SPY": date(2026, 7, 16)},
            "skipped_tickers": [],
        }

    def fake_run_etf_flow_fetch(engine, *, tickers, dry_run, start_date, end_date, allow_yfinance_fallback):
        calls.append(("fetch", engine, tickers, dry_run, start_date, end_date, allow_yfinance_fallback))
        return {"rows": 3, "upserted": 3, "recomputed": 6, "sample": []}

    def fake_run_etf_flow_analytics(engine, *, as_of_date, start_date, dry_run, write_report_output):
        calls.append(("analytics", engine, as_of_date, start_date, dry_run, write_report_output))
        return {"as_of_date": "2026-07-17", "raw_rows": 3, "feature_rows": 3, "representative_signal_rows": 3}

    def fake_run_positioning_flow_signal_update(engine, *, dry_run):
        calls.append(("signals", engine, dry_run))
        return {"rows": 2, "upserted": 2}

    import db_builder.etf_flow.run as etf_flow_run
    import db_builder.etf_flows as etf_flows
    import db_builder.positioning_flow_signals as positioning_flow_signals

    monkeypatch.setattr(macro_data_fetch, "local_engine", object())
    monkeypatch.setattr(positioning_flow_signals, "setup_flow_tables", fake_setup_flow_tables)
    monkeypatch.setattr(etf_flows, "plan_etf_flow_missing_fetch", fake_plan_etf_flow_missing_fetch)
    monkeypatch.setattr(etf_flows, "run_etf_flow_fetch", fake_run_etf_flow_fetch)
    monkeypatch.setattr(etf_flow_run, "run_etf_flow_analytics", fake_run_etf_flow_analytics)
    monkeypatch.setattr(positioning_flow_signals, "run_positioning_flow_signal_update", fake_run_positioning_flow_signal_update)

    result = macro_data_fetch.run_etf_flow_update_after_macro(
        start_date=date(2026, 1, 1),
        tickers=["spy", "ivv"],
        dry_run=False,
    )

    assert result["upserted"] == 3
    assert calls[0][0] == "setup"
    assert calls[1][0] == "plan"
    assert calls[2][0] == "fetch"
    assert calls[2][2] == ["SPY", "IVV"]
    assert calls[2][3] is False
    assert calls[2][4] == date(2026, 1, 1)
    assert calls[2][5] == date(2026, 7, 17)
    assert calls[2][6] is False
    assert calls[3][0] == "analytics"
    assert calls[4][0] == "signals"


def test_macro_etf_flow_post_step_dry_run_does_not_refresh_signals(monkeypatch):
    calls = []

    def fake_setup_flow_tables(engine):
        calls.append(("setup", engine))

    def fake_plan_etf_flow_missing_fetch(engine, *, tickers, start_date):
        calls.append(("plan", tickers, start_date))
        return {
            "target_date": date(2026, 7, 17),
            "start_date": date(2026, 7, 16),
            "tickers": ["SPY"],
            "latest_dates": {},
            "skipped_tickers": [],
        }

    def fake_run_etf_flow_fetch(engine, *, tickers, dry_run, start_date, end_date, allow_yfinance_fallback):
        calls.append(("fetch", tickers, dry_run, start_date, end_date, allow_yfinance_fallback))
        return {"rows": 1, "upserted": 0, "recomputed": 0, "sample": []}

    def fake_run_positioning_flow_signal_update(engine, *, dry_run):
        calls.append(("signals", dry_run))
        return {"rows": 0, "upserted": 0}

    import db_builder.etf_flows as etf_flows
    import db_builder.positioning_flow_signals as positioning_flow_signals

    monkeypatch.setattr(macro_data_fetch, "local_engine", object())
    monkeypatch.setattr(positioning_flow_signals, "setup_flow_tables", fake_setup_flow_tables)
    monkeypatch.setattr(etf_flows, "plan_etf_flow_missing_fetch", fake_plan_etf_flow_missing_fetch)
    monkeypatch.setattr(etf_flows, "run_etf_flow_fetch", fake_run_etf_flow_fetch)
    monkeypatch.setattr(positioning_flow_signals, "run_positioning_flow_signal_update", fake_run_positioning_flow_signal_update)

    macro_data_fetch.run_etf_flow_update_after_macro(dry_run=True)

    assert calls == [
        ("plan", None, None),
        ("fetch", ["SPY"], True, date(2026, 7, 16), date(2026, 7, 17), False),
    ]
