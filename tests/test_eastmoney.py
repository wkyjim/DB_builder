from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import requests

from db_builder import eastmoney


def stock(ticker: str) -> dict:
    return {
        "f12": ticker,
        "f14": f"Name {ticker}",
        "f17": 10,
        "f15": 11,
        "f16": 9,
        "f2": 10.5,
        "f4": 0.5,
        "f3": 5,
        "f18": 10,
        "f6": 1000,
        "f5": 100,
        "f20": 10000,
        "f13": 105,
        "f24": 2,
        "f115": 15,
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def request_page(url: str) -> tuple[int, int]:
    query = parse_qs(urlparse(url).query)
    return int(query["pn"][0]), int(query["pz"][0])


def configure_fetch(monkeypatch, responder):
    monkeypatch.setattr(eastmoney, "_load_target_table", lambda engine, table: object())
    monkeypatch.setattr(eastmoney.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(eastmoney.random, "uniform", lambda start, end: 0)
    monkeypatch.setattr(eastmoney.requests, "get", responder)
    monkeypatch.setattr(
        eastmoney.pd,
        "read_sql_query",
        lambda *args, **kwargs: __import__("pandas").DataFrame(
            columns=["ticker", "prior_ytd_pct_chg", "prior_close"]
        ),
    )
    monkeypatch.setattr(
        eastmoney,
        "fetch_prior_session_universe",
        lambda engine, target_date, table_name: __import__("pandas").DataFrame(
            columns=["ticker", "name", "market", "close", "mkt_cap", "pe_ttm"]
        ),
    )


def test_full_fetch_visits_and_upserts_every_page(monkeypatch):
    calls = []
    upserts = []

    def responder(url, **kwargs):
        page, page_size = request_page(url)
        calls.append((page, page_size))
        if page_size == 1:
            return Response({"data": {"total": 201, "diff": [stock("INIT")]}})
        start = (page - 1) * 100
        count = 1 if page == 3 else 100
        return Response(
            {
                "data": {
                    "total": 201,
                    "diff": [stock(f"T{index:03d}") for index in range(start, start + count)],
                }
            }
        )

    configure_fetch(monkeypatch, responder)
    monkeypatch.setattr(
        eastmoney,
        "_upsert_dataframe",
        lambda engine, table, frame: upserts.append(frame.copy()),
    )

    eastmoney.fetch_and_save_all(
        engine=object(),
        expected_session_date="2026-07-08",
    )

    assert calls == [(1, 1), (1, 100), (2, 100), (3, 100)]
    assert [len(frame) for frame in upserts] == [100, 100, 1]


def test_failed_page_raises_instead_of_reporting_success(monkeypatch):
    def responder(url, **kwargs):
        page, page_size = request_page(url)
        if page_size == 1:
            return Response({"data": {"total": 101, "diff": [stock("INIT")]}})
        if page == 2:
            raise requests.ConnectionError("network down")
        return Response(
            {"data": {"total": 101, "diff": [stock(f"T{index:03d}") for index in range(100)]}}
        )

    configure_fetch(monkeypatch, responder)
    monkeypatch.setattr(eastmoney, "_upsert_dataframe", lambda *args: None)

    with pytest.raises(RuntimeError, match="failed pages"):
        eastmoney.fetch_and_save_all(
            engine=object(),
            expected_session_date="2026-07-08",
        )


def test_incomplete_unique_ticker_coverage_raises(monkeypatch):
    def responder(url, **kwargs):
        page, page_size = request_page(url)
        if page_size == 1:
            return Response({"data": {"total": 100, "diff": [stock("INIT")]}})
        return Response({"data": {"total": 100, "diff": [stock("ONLY")]}})

    configure_fetch(monkeypatch, responder)
    monkeypatch.setattr(eastmoney, "_upsert_dataframe", lambda *args: None)

    with pytest.raises(RuntimeError, match="coverage"):
        eastmoney.fetch_and_save_all(
            engine=object(),
            expected_session_date="2026-07-08",
        )


def test_missing_prior_session_ticker_uses_yfinance_fallback(monkeypatch):
    import pandas as pd
    from db_builder.yfinance_equity_fallback import YFinanceFallbackResult

    def responder(url, **kwargs):
        page, page_size = request_page(url)
        if page_size == 1:
            return Response({"data": {"total": 2, "diff": [stock("INIT")]}})
        return Response({"data": {"total": 2, "diff": [stock("AAA")]}})

    configure_fetch(monkeypatch, responder)
    monkeypatch.setattr(
        eastmoney,
        "fetch_prior_session_universe",
        lambda *args: pd.DataFrame(
            [
                {"ticker": "AAA", "name": "AAA", "market": "105", "close": 10},
                {"ticker": "BBB", "name": "BBB", "market": "105", "close": 20},
            ]
        ),
    )
    fallback_frame = pd.DataFrame([stock("BBB")]).rename(columns=eastmoney.FIELDS_MAP)
    fallback_frame["date"] = pd.Timestamp("2026-07-08").date()
    monkeypatch.setattr(
        eastmoney,
        "fetch_missing_session_rows",
        lambda *args, **kwargs: YFinanceFallbackResult(
            fallback_frame,
            ("BBB",),
            ("BBB",),
            (),
        ),
    )
    upserts = []
    monkeypatch.setattr(
        eastmoney,
        "_upsert_dataframe",
        lambda engine, table, frame: upserts.append(frame.copy()),
    )

    eastmoney.fetch_and_save_all(
        engine=object(),
        expected_session_date="2026-07-08",
    )

    assert len(upserts) == 2
    assert "BBB" in set(upserts[-1]["ticker"])


def test_calculate_ytd_pct_chg_compounds_prior_local_ytd():
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "close": 110,
                "pct_chg": 10,
                "prev_close": 100,
            }
        ]
    )
    prior = pd.DataFrame(
        [{"ticker": "AAA", "prior_ytd_pct_chg": 20, "prior_close": 100}]
    )

    ytd = eastmoney._calculate_ytd_pct_chg_from_prior(frame, prior)

    assert ytd.iloc[0] == 32.0


def test_calculate_ytd_pct_chg_uses_close_return_when_daily_pct_missing():
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "close": 105,
                "pct_chg": None,
                "prev_close": 100,
            }
        ]
    )
    prior = pd.DataFrame(
        [{"ticker": "AAA", "prior_ytd_pct_chg": 10, "prior_close": 100}]
    )

    ytd = eastmoney._calculate_ytd_pct_chg_from_prior(frame, prior)

    assert ytd.iloc[0] == 15.5


def test_calculate_ytd_pct_chg_first_available_defaults_to_daily_return():
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "ticker": "NEW",
                "close": 10.5,
                "pct_chg": 5,
                "prev_close": 10,
            }
        ]
    )
    prior = pd.DataFrame(columns=["ticker", "prior_ytd_pct_chg", "prior_close"])

    ytd = eastmoney._calculate_ytd_pct_chg_from_prior(frame, prior)

    assert ytd.iloc[0] == 5.0
