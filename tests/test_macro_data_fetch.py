from datetime import date, datetime
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

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


def test_replace_macro_live_deduplicates_symbols_before_insert():
    executions = []

    class FakeConnection:
        def execute(self, statement, parameters=None):
            executions.append((str(statement), parameters))

    class FakeTransaction:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeEngine:
        def begin(self):
            return FakeTransaction()

    rows = [
        {"symbol": "^VIX", "close": 14.1},
        {"symbol": "^VIX", "close": 14.2},
        {"symbol": "ES=F", "close": 7800.0},
    ]

    macro_data_fetch.replace_macro_live(FakeEngine(), rows)

    assert "pg_advisory_xact_lock" in executions[0][0]
    inserted = executions[2][1]
    assert len(inserted) == 2
    assert {row["symbol"] for row in inserted} == {"^VIX", "ES=F"}
    assert next(row for row in inserted if row["symbol"] == "^VIX")["close"] == 14.2


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


def test_investiny_403_uses_exponential_cooldown(monkeypatch):
    monkeypatch.setattr(macro_data_fetch.random, "uniform", lambda *_args: 0.0)

    assert macro_data_fetch._investiny_retry_delay("HTTP 403", 1) == 8.0
    assert macro_data_fetch._investiny_retry_delay("HTTP 403", 2) == 16.0
    assert macro_data_fetch._investiny_retry_delay("HTTP 403", 4) == 60.0


def test_fetch_investiny_symbol_df_recovers_after_403(monkeypatch):
    attempts = []
    sleeps = []

    def fake_historical_data(**_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("Request failed with error code: 403")
        return {
            "date": ["07/01/2026"],
            "open": [4.4],
            "high": [4.6],
            "low": [4.3],
            "close": [4.55],
        }

    monkeypatch.setitem(
        sys.modules,
        "investiny",
        types.SimpleNamespace(historical_data=fake_historical_data),
    )
    monkeypatch.setattr(macro_data_fetch.time, "sleep", sleeps.append)
    monkeypatch.setattr(macro_data_fetch.random, "uniform", lambda *_args: 0.0)

    df, _ = macro_data_fetch.fetch_investiny_symbol_df(
        "US10YT=X",
        start_date=date(2026, 7, 1),
    )

    assert len(attempts) == 3
    assert sleeps == [0.0, 8.0, 0.0, 16.0, 0.0]
    assert df.iloc[0]["Close"] == 4.55


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

    _rows, live_row, rows_fetched, latest_observation = macro_data_fetch.process_investiny_symbol(
        "BZ=F",
        {"name": "Brent Crude Oil Future", "asset_type": "futures"},
    )

    assert live_row["source"] == "investing.com"
    assert rows_fetched == 2
    assert latest_observation == date(2026, 7, 8)


def test_process_investiny_failure_preserves_fetched_row_count(monkeypatch):
    one_row = sample_bars().iloc[:1].copy()
    monkeypatch.setattr(
        macro_data_fetch,
        "fetch_investiny_symbol_df",
        lambda *_args, **_kwargs: (one_row, date(2026, 7, 7)),
    )

    with pytest.raises(macro_data_fetch.SymbolProcessingError) as exc_info:
        macro_data_fetch.process_investiny_symbol(
            "US10YT=X",
            {"name": "United States 10-Year Treasury Yield", "asset_type": "ust_yield"},
        )

    assert exc_info.value.rows_fetched == 1
    assert exc_info.value.latest_observation is None
    assert "insufficient provider observations" in str(exc_info.value)


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


def make_outcome(symbol, *, success=True, provider=None, reason=None):
    asset = next(item for item in macro_data_fetch.ASSETS if item[0] == symbol)
    return macro_data_fetch.SymbolFetchOutcome(
        symbol=symbol,
        name=asset[1],
        provider=provider or macro_data_fetch.provider_for_symbol(symbol),
        required=symbol in macro_data_fetch.REQUIRED_SYMBOLS,
        success=success,
        rows_fetched=2 if success else 0,
        latest_observation=date(2026, 9, 17) if success else None,
        failure_reason=reason,
    )


def configure_macro_run(monkeypatch, *, yf_failures=None, investiny_failures=None):
    yf_failures = yf_failures or {}
    investiny_failures = investiny_failures or {}
    live_actions = []

    monkeypatch.setattr(macro_data_fetch.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(macro_data_fetch, "create_macro_table", lambda *_args: None)
    monkeypatch.setattr(macro_data_fetch, "create_macro_live_table", lambda *_args: None)
    monkeypatch.setattr(
        macro_data_fetch,
        "download_recent_batch",
        lambda *_args, **_kwargs: (pd.DataFrame({"placeholder": [1]}), date(2026, 9, 12)),
    )

    def fake_extract(_batch_df, symbol):
        failure = yf_failures.get(symbol)
        if failure == "empty":
            return pd.DataFrame()
        if failure == "malformed":
            return pd.DataFrame({"Date": [pd.Timestamp("2026-09-17")]})
        if isinstance(failure, Exception):
            raise failure
        return sample_bars()

    def fake_process_investiny(symbol, _meta, **_kwargs):
        failure = investiny_failures.get(symbol)
        if failure:
            raise failure
        return (
            [{"date": date(2026, 7, 8), "symbol": symbol}],
            None,
            2,
            date(2026, 7, 8),
        )

    monkeypatch.setattr(macro_data_fetch, "extract_symbol_df_from_batch", fake_extract)
    monkeypatch.setattr(macro_data_fetch, "extract_latest_unfinished_row", lambda **_kwargs: None)
    monkeypatch.setattr(macro_data_fetch, "remove_unfinished_bars", lambda df, _symbol: df)
    monkeypatch.setattr(
        macro_data_fetch,
        "calculate_rows_for_symbol",
        lambda df, symbol, name, asset_type: [
            {"date": date(2026, 7, 8), "symbol": symbol}
        ],
    )
    monkeypatch.setattr(
        macro_data_fetch,
        "filter_rows_to_target_window",
        lambda rows, target_start_date: rows,
    )
    monkeypatch.setattr(macro_data_fetch, "process_investiny_symbol", fake_process_investiny)
    monkeypatch.setattr(macro_data_fetch, "upsert_new_macro_rows", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        macro_data_fetch,
        "replace_macro_live",
        lambda _engine, rows: live_actions.append(("replace", list(rows))),
    )
    monkeypatch.setattr(
        macro_data_fetch,
        "merge_macro_live",
        lambda _engine, rows, symbols: live_actions.append(
            ("merge", list(rows), set(symbols))
        ),
    )
    return live_actions


def test_required_symbol_policy_is_explicit_and_valid():
    configured = {symbol for symbol, _name, _asset_type in macro_data_fetch.ASSETS}

    assert macro_data_fetch.REQUIRED_SYMBOLS <= configured
    assert {"^GSPC", "CL=F", "BZ=F", "US2YT=X", "US10YT=X", "US30YT=X"} <= (
        macro_data_fetch.REQUIRED_SYMBOLS
    )
    assert macro_data_fetch.MIN_COVERAGE_PCT == 90.0


def test_coverage_summary_all_provider_success():
    outcomes = [make_outcome(symbol) for symbol, _name, _asset_type in macro_data_fetch.ASSETS]

    summary = macro_data_fetch.summarize_outcomes(outcomes)

    assert summary.configured_count == len(macro_data_fetch.ASSETS)
    assert summary.successful_count == len(macro_data_fetch.ASSETS)
    assert summary.failed_count == 0
    assert summary.coverage_pct == 100.0
    assert summary.provider_failure_counts == (("investing.com", 0), ("yfinance", 0))
    assert summary.exit_code == 0


def test_run_with_yfinance_and_investing_success_logs_success(monkeypatch, capsys):
    configure_macro_run(monkeypatch)

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["^GSPC", "US10YT=X"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    output = capsys.readouterr().out
    assert summary.configured_count == 2
    assert summary.successful_count == 2
    assert summary.exit_code == 0
    assert "[MACRO UPDATE SUCCESS]" in output
    assert "[MACRO UPDATE PARTIAL FAILURE]" not in output


def test_required_symbol_failure_is_not_accepted():
    outcomes = [make_outcome("^GSPC", success=False, reason="empty provider response")]

    summary = macro_data_fetch.summarize_outcomes(outcomes)

    assert summary.required_failed_symbols == ("^GSPC",)
    assert summary.coverage_satisfied is False
    assert summary.exit_code == 1


def test_supplementary_only_failure_within_threshold_is_accepted():
    supplementary = [
        symbol
        for symbol, _name, _asset_type in macro_data_fetch.ASSETS
        if symbol not in macro_data_fetch.REQUIRED_SYMBOLS
    ][:10]
    outcomes = [make_outcome(symbol, success=symbol != supplementary[-1]) for symbol in supplementary]

    summary = macro_data_fetch.summarize_outcomes(outcomes)

    assert summary.coverage_pct == 90.0
    assert summary.required_failed_symbols == ()
    assert summary.coverage_satisfied is True
    assert summary.failed_count == 1
    assert summary.exit_code == 0


def test_accepted_supplementary_failure_logs_partial_failure(monkeypatch, capsys):
    symbols = [
        "^IXIC",
        "^RUT",
        "^SKEW",
        "^MOVE",
        "^KS200",
        "000001.SS",
        "^FTSE",
        "^GDAXI",
        "^FCHI",
        "NQ=F",
    ]
    configure_macro_run(monkeypatch, yf_failures={"^SKEW": "empty"})

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=symbols,
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    output = capsys.readouterr().out
    assert summary.coverage_pct == 90.0
    assert summary.exit_code == 0
    assert "[MACRO UPDATE PARTIAL FAILURE]" in output
    assert "[MACRO UPDATE SUCCESS]" not in output


def test_partial_supplementary_failure_below_coverage_is_rejected():
    supplementary = [
        symbol
        for symbol, _name, _asset_type in macro_data_fetch.ASSETS
        if symbol not in macro_data_fetch.REQUIRED_SYMBOLS
    ][:10]
    outcomes = [make_outcome(symbol, success=index < 8) for index, symbol in enumerate(supplementary)]

    summary = macro_data_fetch.summarize_outcomes(outcomes)

    assert summary.coverage_pct == 80.0
    assert summary.required_failed_symbols == ()
    assert summary.coverage_satisfied is False
    assert summary.exit_code == 1


def test_all_investing_failures_return_nonzero_and_do_not_replace_live(monkeypatch, capsys):
    failures = {
        symbol: ConnectionError("Request failed with error code: 403")
        for symbol in macro_data_fetch.INVESTINY_ASSETS
    }
    live_actions = configure_macro_run(monkeypatch, investiny_failures=failures)

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=list(macro_data_fetch.INVESTINY_ASSETS),
        upsert_neon=False,
        update_etf_flows=False,
    )

    output = capsys.readouterr().out
    assert summary.failed_count == 12
    assert summary.provider_failure_counts == (("investing.com", 12),)
    assert summary.exit_code == 1
    assert "[MACRO UPDATE FAILURE]" in output
    assert "SUCCESS" not in output
    assert all(action[0] == "merge" for action in live_actions)
    assert all(action[2] == set() for action in live_actions)


def test_partial_investing_failure_below_required_coverage_returns_nonzero(monkeypatch):
    successful = {"CL=F", "BZ=F"}
    failures = {
        symbol: ConnectionError("403")
        for symbol in macro_data_fetch.INVESTINY_ASSETS
        if symbol not in successful
    }
    configure_macro_run(monkeypatch, investiny_failures=failures)

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=list(macro_data_fetch.INVESTINY_ASSETS),
        upsert_neon=False,
        update_etf_flows=False,
        update_live=False,
    )

    assert summary.successful_count == 2
    assert summary.failed_count == 10
    assert summary.coverage_pct < macro_data_fetch.MIN_COVERAGE_PCT
    assert summary.exit_code == 1


def test_yfinance_batch_exception_is_recorded_and_returns_nonzero(monkeypatch):
    configure_macro_run(monkeypatch)
    monkeypatch.setattr(
        macro_data_fetch,
        "download_recent_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("provider unavailable")),
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["^GSPC", "^SKEW"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    assert summary.failed_count == 2
    assert all(item.failure_reason == "ConnectionError: provider unavailable" for item in summary.outcomes)
    assert summary.exit_code == 1


def test_investing_database_write_error_remains_fatal(monkeypatch):
    configure_macro_run(monkeypatch)
    monkeypatch.setattr(
        macro_data_fetch,
        "upsert_new_macro_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        macro_data_fetch.run_daily_update(
            symbol_filter=["US10YT=X"],
            upsert_neon=False,
            update_etf_flows=False,
            update_live=False,
        )


def test_empty_and_malformed_provider_responses_are_failures(monkeypatch):
    configure_macro_run(
        monkeypatch,
        yf_failures={"^GSPC": "empty", "^SKEW": "malformed"},
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["^GSPC", "^SKEW"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    outcomes = {item.symbol: item for item in summary.outcomes}
    assert outcomes["^GSPC"].failure_reason == "ValueError: empty provider response"
    assert outcomes["^SKEW"].failure_reason == (
        "ValueError: malformed provider response: Date/Close columns required"
    )
    assert summary.exit_code == 1


def test_partial_live_merge_retains_failed_symbol_and_original_timestamp():
    executions = []

    class FakeConnection:
        def execute(self, statement, parameters=None):
            executions.append((str(statement), parameters))

    class FakeTransaction:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeEngine:
        def begin(self):
            return FakeTransaction()

    fresh_row = {
        "symbol": "^GSPC",
        "observed_at": datetime(2026, 9, 18, tzinfo=ZoneInfo("UTC")),
    }
    macro_data_fetch.merge_macro_live(FakeEngine(), [fresh_row], {"^GSPC"})

    assert "WHERE symbol = ANY" in executions[1][0]
    assert executions[1][1] == {"symbols": ["^GSPC"]}
    delete_statements = [statement for statement, _parameters in executions if "DELETE FROM" in statement]
    assert len(delete_statements) == 1
    assert "WHERE symbol = ANY" in delete_statements[0]
    assert executions[2][1][0]["observed_at"] == fresh_row["observed_at"]
    assert all("US10YT=X" not in str(parameters) for _statement, parameters in executions)


def test_coverage_summary_logging_is_deterministic(capsys):
    outcomes = [
        make_outcome("^SKEW", success=False, reason="empty provider response"),
        make_outcome("^GSPC"),
    ]
    summary = macro_data_fetch.summarize_outcomes(outcomes)

    macro_data_fetch.log_coverage_summary(summary)

    lines = capsys.readouterr().out.splitlines()
    assert "symbol=^GSPC" in lines[0]
    assert "symbol=^SKEW" in lines[1]
    assert "configured=2 successful=1 failed=1 coverage=50.00%" in lines[2]
    assert lines[3] == "[MACRO FAILED SYMBOLS] ^SKEW(CBOE SKEW Index)"
    assert lines[-1] == "[MACRO PROVIDER FAILURES] provider=yfinance failed=1"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_cli_main_propagates_coverage_exit_code(monkeypatch, exit_code):
    monkeypatch.setattr(
        macro_data_fetch,
        "run_daily_update",
        lambda **_kwargs: SimpleNamespace(exit_code=exit_code),
    )

    assert macro_data_fetch.main(["--skip-etf-flows", "--skip-live-replace"]) == exit_code
