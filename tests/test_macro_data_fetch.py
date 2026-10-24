from datetime import date, datetime
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import dataclasses

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import os
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a-test")

from scripts import macro_data_fetch


def test_macro_fetch_registry_includes_dashboard_tape_symbols():
    symbols = {symbol for symbol, _, _ in macro_data_fetch.ASSETS}
    expected = {
        "^NDX",
        "^HSI",
        "NIY=F",
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
    } <= set(macro_data_fetch.INVESTINY_ASSETS)
    # BZ=F/CL=F are yfinance-owned, so no Investing source symbol remains.
    assert macro_data_fetch.INVESTING_SOURCE_SYMBOLS == {}


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
    """The Investing.com path is HTML-only: the scraper returns
    {date, price, open, high, low} and the pipeline normalizes it to the
    standard frame (Date/Open/High/Low/Close/Adj Close/Volume)."""

    def fake_fetch(symbol, timeout=45, start_date=None, end_date=None):
        assert symbol == "US10YT=X"
        return [
            {"date": "2026-07-01", "price": 4.55, "open": 4.4, "high": 4.6, "low": 4.3},
            {"date": "2026-07-02", "price": 4.65, "open": 4.5, "high": 4.7, "low": 4.4},
        ]

    monkeypatch.setattr(macro_data_fetch._investing_html, "fetch_historical_data", fake_fetch)
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
    def fake_fetch(symbol, timeout=45, start_date=None, end_date=None):
        # 2026-07-05 is a Sunday
        return [
            {"date": "2026-07-03", "price": 4.55, "open": 4.4, "high": 4.6, "low": 4.3},
            {"date": "2026-07-05", "price": 4.65, "open": 4.5, "high": 4.7, "low": 4.4},
            {"date": "2026-07-06", "price": 4.75, "open": 4.6, "high": 4.8, "low": 4.5},
        ]

    monkeypatch.setattr(macro_data_fetch._investing_html, "fetch_historical_data", fake_fetch)
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


def test_fetch_investiny_symbol_df_hard_failure_raises_after_retries(monkeypatch):
    """A provider-level failure (the HTML page never returns usable rows) must
    surface as InvestinyProviderUnavailable after the retry budget is spent, so
    the cycle-level circuit breaker can route the symbol to its fallback provider.

    The old 403-specific fast path is gone: the HTML scraper uses a browser TLS
    fingerprint, so Investing.com no longer answers with HTTP 403. A blocked or
    empty page now looks like any other hard fetch failure."""
    attempts = []
    sleeps = []

    def fake_fetch(symbol, timeout=45, start_date=None, end_date=None):
        attempts.append(1)
        raise ConnectionError("provider unavailable")

    monkeypatch.setattr(macro_data_fetch._investing_html, "fetch_historical_data", fake_fetch)
    monkeypatch.setattr(macro_data_fetch.time, "sleep", sleeps.append)
    monkeypatch.setattr(macro_data_fetch.random, "uniform", lambda *_args: 0.0)

    with pytest.raises(macro_data_fetch.InvestinyProviderUnavailable):
        macro_data_fetch.fetch_investiny_symbol_df(
            "US10YT=X",
            start_date=date(2026, 7, 1),
        )

    # Retry budget is honoured (default max_retries=2), then it raises.
    # Sleeps = 2 pre-call jitter + 1 inter-attempt cooldown.
    assert len(attempts) == 2
    assert len(sleeps) == 3


def test_fetch_investiny_symbol_df_recovers_after_transient_error(monkeypatch):
    """Transient errors still retry (max 2 attempts) and recover."""
    attempts = []
    sleeps = []

    def fake_fetch(symbol, timeout=45, start_date=None, end_date=None):
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("timed out")
        return [
            {"date": "2026-07-01", "price": 4.55, "open": 4.4, "high": 4.6, "low": 4.3},
        ]

    monkeypatch.setattr(macro_data_fetch._investing_html, "fetch_historical_data", fake_fetch)
    monkeypatch.setattr(macro_data_fetch.time, "sleep", sleeps.append)
    monkeypatch.setattr(macro_data_fetch.random, "uniform", lambda *_args: 0.0)

    df, _ = macro_data_fetch.fetch_investiny_symbol_df(
        "US10YT=X",
        start_date=date(2026, 7, 1),
    )

    assert len(attempts) == 2
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
        lambda engine, new_rows, **kwargs: writes.append((engine, new_rows)),
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


# Captured BEFORE any test mocks it: the genuine row constructor.
_REAL_CALCULATE_ROWS = macro_data_fetch.calculate_rows_for_symbol


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


def configure_macro_run(monkeypatch, *, yf_failures=None, investiny_failures=None,
                        treasury_payload=None, treasury_failure=None):
    yf_failures = yf_failures or {}
    investiny_failures = investiny_failures or {}
    live_actions = []

    monkeypatch.setattr(macro_data_fetch.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        macro_data_fetch,
        "latest_completed_nyse_session_date",
        lambda **_kwargs: date(2026, 7, 8),
    )
    monkeypatch.setattr(macro_data_fetch, "create_macro_table", lambda *_args: None)
    monkeypatch.setattr(macro_data_fetch, "create_macro_live_table", lambda *_args: None)
    monkeypatch.setattr(
        macro_data_fetch,
        "download_recent_batch",
        lambda *_args, **_kwargs: (pd.DataFrame({"placeholder": [1]}), date(2026, 9, 12)),
    )

    # P1B: Treasury XML fetch mock. treasury_payload maps year -> DataFrame
    # (or a single DataFrame used for every year). treasury_failure raises.
    treasury_fetch_calls = []
    _DEFAULT_TREASURY = pd.DataFrame(
        {
            "BC_2YEAR": [3.47, 3.46, 3.48],
            "BC_3YEAR": [3.53, 3.52, 3.54],
            "BC_5YEAR": [3.71, 3.70, 3.72],
            "BC_7YEAR": [3.92, 3.91, 3.93],
            "BC_10YEAR": [4.17, 4.16, 4.18],
            "BC_20YEAR": [4.79, 4.78, 4.80],
            "BC_30YEAR": [4.85, 4.84, 4.86],
        },
        index=pd.to_datetime(
            ["2026-07-06", "2026-07-07", "2026-07-08"], utc=True
        ),
    )

    def fake_fetch_treasury(year):
        treasury_fetch_calls.append(year)
        if treasury_failure is not None:
            raise treasury_failure
        if treasury_payload is None:
            return _DEFAULT_TREASURY.copy()
        if isinstance(treasury_payload, pd.DataFrame):
            return treasury_payload.copy()
        return treasury_payload.get(year, _DEFAULT_TREASURY).copy()

    monkeypatch.setattr(
        macro_data_fetch, "fetch_treasury_par_yields", fake_fetch_treasury
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
        lambda df, symbol, name, asset_type, **kwargs: [
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
    # Phase A (additive): the official CMT series adds a third provider.
    assert summary.provider_failure_counts == (
        ("investing.com", 0), ("oilpriceapi.com", 0),
        ("treasury.gov", 0), ("yfinance", 0))
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
    """A failure confined to non-required symbols must not fail the run.

    REQUIRED_SYMBOLS now covers every asset, so the supplementary set cannot be
    derived from the universe any more. Build it directly with required=False
    and keep the same invariant: 9 of 10 succeed, nothing required failed.
    """
    symbols = (
        "^IXIC", "^RUT", "^SKEW", "^MOVE", "000001.SS",
        "^FTSE", "^GDAXI", "^FCHI", "NQ=F", "YM=F",
    )
    outcomes = [
        dataclasses.replace(
            make_outcome(symbol), required=False, success=(symbol != symbols[-1])
        )
        for symbol in symbols
    ]

    summary = macro_data_fetch.summarize_outcomes(outcomes)

    assert summary.coverage_pct == 90.0
    assert summary.required_failed_symbols == ()
    assert summary.coverage_satisfied is True
    assert summary.failed_count == 1
    assert summary.exit_code == 0


def test_required_failure_at_coverage_threshold_still_fails(monkeypatch, capsys):
    """A required symbol failing at the coverage threshold must still fail.

    The original scenario assumed ^SKEW was a supplementary symbol, whose
    failure could be tolerated. REQUIRED_SYMBOLS now covers every asset, so a
    single required failure fails the run even when coverage sits exactly on
    the 90% threshold -- and it must be reported as such.
    """
    symbols = [
        "^IXIC",
        "^RUT",
        "^SKEW",
        "^MOVE",
        "000001.SS",
        "^FTSE",
        "^GDAXI",
        "^FCHI",
        "NQ=F",
        "YM=F",
    ]
    configure_macro_run(monkeypatch, yf_failures={"^SKEW": "empty"})

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=symbols,
        upsert_neon=False,
        dry_run=False,
        update_etf_flows=False,
    )

    output = capsys.readouterr().out
    assert summary.coverage_pct == 90.0
    assert summary.required_failed_symbols == ("^SKEW",)
    assert summary.coverage_satisfied is False
    assert summary.exit_code == 1
    assert "[MACRO UPDATE FAILURE]" in output
    assert "SUCCESS" not in output
    assert "[MACRO FAILED SYMBOLS]" in output


def test_partial_supplementary_failure_below_coverage_is_rejected():
    """Supplementary failures below the coverage threshold must fail the run.

    Same construction as the accepted case above: 8 of 10 supplementary symbols
    succeed, which is 80% -- below the 90% threshold -- so the run is rejected
    even though no required symbol failed.
    """
    symbols = (
        "^IXIC", "^RUT", "^SKEW", "^MOVE", "000001.SS",
        "^FTSE", "^GDAXI", "^FCHI", "NQ=F", "YM=F",
    )
    outcomes = [
        dataclasses.replace(
            make_outcome(symbol), required=False, success=(index < 8)
        )
        for index, symbol in enumerate(symbols)
    ]

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
    # With Investing.com fully down, the three Asia futures recover through the
    # yfinance cash-index proxy (KOR200c1 <- ^KS11, HK50 <- ^HSI, CIHc1 <- 000001.SS)
    # while the 7 UST yields have no working fallback and fail.
    assert summary.successful_count == 3
    assert summary.failed_count == 7
    assert summary.provider_failure_counts == (
        ("investing.com", 10),
        ("yfinance (cash index proxy)", 0),
    )
    assert summary.exit_code == 1
    assert "[MACRO UPDATE FAILURE]" in output
    assert "SUCCESS" not in output
    # Live data must be preserved (merged), never replaced.
    assert all(action[0] == "merge" for action in live_actions)
    assert all(
        action[2] == {"CIHc1", "HK50", "KOR200c1"} for action in live_actions
    )


@pytest.mark.parametrize("symbol", ["HK50", "KOR200c1"])
def test_oil_investing_403_falls_back_to_yfinance_with_canonical_symbol(
    monkeypatch,
    capsys,
    symbol,
):
    configure_macro_run(
        monkeypatch,
        investiny_failures={symbol: ConnectionError("Request failed with error code: 403")},
    )
    writes = []
    monkeypatch.setattr(
        macro_data_fetch,
        "upsert_new_macro_rows",
        lambda rows, symbols, **kwargs: writes.append((rows, symbols, kwargs)) or 1,
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=[symbol],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    output = capsys.readouterr().out
    outcome = summary.outcomes[0]
    assert summary.exit_code == 0
    assert summary.coverage_pct == 100.0
    assert outcome.success is True
    assert outcome.provider == "yfinance (cash index proxy)"
    assert outcome.primary_provider == "investing.com"
    assert outcome.primary_failure_reason == "ConnectionError: Request failed with error code: 403"
    assert outcome.fallback_provider == "yfinance (cash index proxy)"
    assert outcome.fallback_failure_reason is None
    assert summary.provider_failure_counts == (
        ("investing.com", 1), ("yfinance (cash index proxy)", 0))
    assert len(writes) == 1
    assert writes[0][0][0]["symbol"] == symbol
    assert writes[0][1] == [symbol]
    assert f"[ASIA PROXY FALLBACK] {symbol}" in output
    assert f"provider=yfinance" in output


@pytest.mark.parametrize(
    "primary_error",
    [
        macro_data_fetch.SymbolProcessingError("ValueError: empty provider response"),
        macro_data_fetch.SymbolProcessingError(
            "ValueError: malformed provider response: Date/Close columns required"
        ),
    ],
)
def test_oil_unusable_primary_response_falls_back_successfully(monkeypatch, primary_error):
    configure_macro_run(
        monkeypatch,
        investiny_failures={"HK50": primary_error},
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["HK50"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    outcome = summary.outcomes[0]
    assert summary.exit_code == 0
    assert outcome.provider == "yfinance (cash index proxy)"
    assert outcome.primary_failure_reason == str(primary_error)


def test_oil_fallback_stale_response_is_rejected(monkeypatch):
    configure_macro_run(
        monkeypatch,
        investiny_failures={"KOR200c1": ConnectionError("403")},
    )
    # The Asia proxy applies a 1-day regional-holiday tolerance, so the required
    # date is pushed 4 days past the response date to exceed that tolerance.
    monkeypatch.setattr(
        macro_data_fetch,
        "latest_completed_nyse_session_date",
        lambda **_kwargs: date(2026, 7, 12),
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["KOR200c1"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    outcome = summary.outcomes[0]
    assert summary.exit_code == 1
    assert outcome.success is False
    assert outcome.primary_failure_reason == "ConnectionError: 403"
    assert "stale Asia proxy response" in outcome.fallback_failure_reason
    assert outcome.latest_observation == date(2026, 7, 8)
    assert summary.provider_failure_counts == (
        ("investing.com", 1), ("yfinance (cash index proxy)", 1))


@pytest.mark.parametrize(
    ("fallback_failure", "expected_reason"),
    [
        ("empty", "empty provider response"),
        ("malformed", "malformed provider response"),
    ],
)
def test_oil_fallback_empty_or_malformed_response_is_rejected(
    monkeypatch,
    fallback_failure,
    expected_reason,
):
    configure_macro_run(
        monkeypatch,
        yf_failures={"^HSI": fallback_failure},
        investiny_failures={"HK50": ValueError("unusable primary response")},
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["HK50"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    outcome = summary.outcomes[0]
    assert summary.exit_code == 1
    assert outcome.success is False
    assert expected_reason in outcome.fallback_failure_reason
    assert "primary investing.com" in outcome.failure_reason
    assert "fallback yfinance" in outcome.failure_reason


def test_oil_fallback_non_finite_close_is_rejected(monkeypatch):
    configure_macro_run(
        monkeypatch,
        investiny_failures={"CIHc1": ConnectionError("403")},
    )
    non_finite = sample_bars()
    non_finite["Close"] = [float("inf"), float("nan")]
    monkeypatch.setattr(
        macro_data_fetch,
        "extract_symbol_df_from_batch",
        lambda _batch_df, _symbol: non_finite,
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["CIHc1"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    outcome = summary.outcomes[0]
    assert summary.exit_code == 1
    assert outcome.success is False
    assert "no finite Date/Close observations" in outcome.fallback_failure_reason


def test_oil_primary_and_fallback_provider_exceptions_return_nonzero(monkeypatch):
    configure_macro_run(
        monkeypatch,
        investiny_failures={"KOR200c1": ConnectionError("investing 403")},
    )
    monkeypatch.setattr(
        macro_data_fetch,
        "process_asia_index_yfinance_fallback_symbol",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("yfinance unavailable")
        ),
    )

    summary = macro_data_fetch.run_daily_update(
        symbol_filter=["KOR200c1"],
        upsert_neon=False,
        dry_run=True,
        update_etf_flows=False,
    )

    outcome = summary.outcomes[0]
    assert summary.exit_code == 1
    assert summary.required_failed_symbols == ("KOR200c1",)
    assert outcome.primary_failure_reason == "ConnectionError: investing 403"
    assert outcome.fallback_failure_reason == "ConnectionError: yfinance unavailable"


def test_oil_fallback_database_write_error_remains_fatal(monkeypatch):
    configure_macro_run(
        monkeypatch,
        investiny_failures={"CL=F": ConnectionError("403")},
    )
    monkeypatch.setattr(
        macro_data_fetch,
        "upsert_new_macro_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        ),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        macro_data_fetch.run_daily_update(
            symbol_filter=["CL=F"],
            upsert_neon=False,
            update_etf_flows=False,
            update_live=False,
        )


# ---------------------------------------------------------------------------
# P1B: U.S. Treasury official par-yield fallback
# ---------------------------------------------------------------------------

UST_SYMBOLS = [
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X",
]


def configure_treasury_freshness(monkeypatch, expected=date(2026, 7, 8),
                                  prev_trading_day=date(2026, 7, 7)):
    """Pin publication-aware freshness to fixed dates for determinism."""
    import db_builder.trading_calendar as tc

    monkeypatch.setattr(
        tc, "expected_latest_treasury_publication", lambda now=None: expected
    )
    monkeypatch.setattr(
        tc, "previous_treasury_trading_day", lambda day=None: prev_trading_day
    )

def test_partial_investing_failure_below_required_coverage_returns_nonzero(monkeypatch):
    # All three Asia symbols have a yfinance cash-index proxy fallback, so they
    # still succeed when Investing.com fails; the seven UST yields have no
    # fallback configured and therefore fail.
    successful = {"HK50", "KOR200c1", "CIHc1"}
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

    assert summary.successful_count == 3
    assert summary.failed_count == 7
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
        lambda **_kwargs: SimpleNamespace(
            exit_code=exit_code,
            coverage_satisfied=(exit_code == 0),
            required_failed_symbols=(),
            required_historical_deferred_symbols=(),
            supplementary_historical_deferred_symbols=(),
            expected_frozen_symbols=(),
            outcomes=(),
            failed_count=0,
        ),
    )

    assert macro_data_fetch.main(["--skip-etf-flows", "--skip-live-replace"]) == exit_code


# ==========================================================================
# Write window: market sessions, not calendar days
# ==========================================================================

class TestWriteWindowUsesMarketSessions:
    """The write window must be measured in *market sessions* on each symbol's
    own exchange calendar, and must keep the prior session of every kept row.

    A calendar-day window silently advances past sessions a market did not
    trade (holiday weeks, long weekends). The historical gate requires D's
    previous session P to be stored before D may be written, so dropping P
    makes any hole in storage permanent -- P can never be written (it is
    outside the window) and D can never be written either.
    """

    # XSHG (000001.SS) was closed 2026-10-05..07 for National Day.
    SYMBOL = "000001.SS"
    # Sessions from 2026-10-08 onward, skipping the holiday and the weekend.
    SESSIONS = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12),
                date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15),
                date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20),
                date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]

    def _rows(self):
        return [{"symbol": self.SYMBOL, "date": d} for d in self.SESSIONS]

    def test_window_boundary_is_a_market_session_not_a_calendar_day(self):
        """Five sessions back from 2026-10-23 on XSHG is 2026-10-16, not the
        calendar-day 2026-10-18. The calendar-day value would have excluded
        10-15/10-16 -- the very sessions the missing rows need."""
        from db_builder.session_calendar import previous_valid_session

        start = macro_data_fetch._session_window_start(
            self.SYMBOL, set(self.SESSIONS), date(2026, 10, 18))

        assert start == date(2026, 10, 16), (
            "window boundary must be the 5th previous XSHG session")
        # and the calendar-day fallback really is coarser
        assert start < date(2026, 10, 18)
        assert previous_valid_session(self.SYMBOL, date(2026, 10, 19)) == \
            date(2026, 10, 16)

    def test_window_keeps_the_prior_of_its_earliest_row(self):
        """A row whose prior is dropped can never be written: the gate requires
        the prior to be stored, and the prior can never be stored because it is
        outside the window. The window must therefore keep priors."""
        kept = macro_data_fetch.filter_rows_to_target_window(
            self._rows(), date(2026, 10, 18))
        kept_dates = {r["date"] for r in kept}

        # the window's earliest session and its prior are both kept
        assert date(2026, 10, 16) in kept_dates
        assert date(2026, 10, 15) in kept_dates
        # and the chain walks back to the frame's oldest session
        assert date(2026, 10, 8) in kept_dates

    def test_window_heals_a_storage_hole(self):
        """Reproduce the 000001.SS deadlock: storage holds 10-08 and 10-14 but
        nothing between, and the window must still surface 10-09 and 10-15 --
        the two rows whose priors ARE stored, so the gate accepts them."""
        stored = {date(2026, 10, 8), date(2026, 10, 14)}
        from db_builder.session_calendar import previous_valid_observation_date

        kept = macro_data_fetch.filter_rows_to_target_window(
            self._rows(), date(2026, 10, 18))

        accepted = []
        for row in kept:
            prior = previous_valid_observation_date(self.SYMBOL, row["date"])
            if prior is not None and prior in stored:
                accepted.append(row["date"])

        assert date(2026, 10, 9) in accepted, "10-09's prior (10-08) is stored"
        assert date(2026, 10, 15) in accepted, "10-15's prior (10-14) is stored"

    def test_window_preserves_caller_row_order(self):
        kept = macro_data_fetch.filter_rows_to_target_window(
            self._rows(), date(2026, 10, 18))
        dates = [r["date"] for r in kept]
        assert dates == sorted(dates), "row order must be preserved"

    def test_window_falls_back_to_calendar_boundary_without_a_calendar(self):
        """A symbol whose calendar cannot resolve must keep the previous
        calendar-day behaviour rather than dropping everything."""
        rows = [{"symbol": "NO_SUCH_SYMBOL", "date": d}
                for d in (date(2026, 10, 19), date(2026, 10, 20))]
        kept = macro_data_fetch.filter_rows_to_target_window(
            rows, date(2026, 10, 18))
        assert {r["date"] for r in kept} == {date(2026, 10, 19),
                                            date(2026, 10, 20)}

    def test_empty_rows_are_returned_unchanged(self):
        assert macro_data_fetch.filter_rows_to_target_window(
            [], date(2026, 10, 18)) == []


