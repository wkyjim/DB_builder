"""Massive grouped-daily equity fetcher."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import requests

from db_builder import massive_equity as me


def test_parse_grouped_bar_maps_ohlcv():
    row = me.parse_grouped_bar(
        {
            "T": "dpz",
            "o": 314.88,
            "h": 317.26,
            "l": 311.6,
            "c": 313.61,
            "v": 443805.06,
            "vw": 313.46,
            "n": 19794,
            "t": 1792785600000,
        }
    )
    assert row == {
        "ticker": "DPZ",
        "open": 314.88,
        "high": 317.26,
        "low": 311.6,
        "close": 313.61,
        "volume": 443805.06,
    }


def test_parse_grouped_bar_drops_unsellable_records():
    assert me.parse_grouped_bar({"T": "", "o": 1, "c": 2}) is None
    assert me.parse_grouped_bar({"o": 1, "c": 2}) is None
    # A bar with no close is not a price row we can store.
    assert me.parse_grouped_bar({"T": "X", "o": 1, "c": None}) is None


def test_parse_grouped_bar_ignores_vwap_and_trade_count():
    """vw/n have no us_equities column, so they must not leak into the row."""
    row = me.parse_grouped_bar({"T": "A", "c": 1.0, "vw": 99.0, "n": 5})
    assert "vw" not in row and "n" not in row


def test_grouped_daily_parses_payload_and_dedupes(monkeypatch):
    payload = {
        "resultsCount": 3,
        "results": [
            {"T": "AAPL", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
            {"T": "aapl", "o": 9, "h": 9, "l": 9, "c": 9, "v": 9},  # dupe, lower case
            {"T": "MSFT", "o": 3, "h": 4, "l": 2.5, "c": 3.5, "v": 200},
        ],
    }

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(me, "_throttle", lambda: None)
    monkeypatch.setattr(me.requests, "get", lambda *a, **k: _Resp())

    frame = me.fetch_grouped_daily("2026-10-23")

    assert len(frame) == 2
    assert set(frame["ticker"]) == {"AAPL", "MSFT"}
    assert frame["date"].iloc[0] == date(2026, 10, 23)


def test_grouped_daily_requires_api_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MASSIVE_API_KEY"):
        me._api_key()


def test_grouped_daily_raises_on_empty_results(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    monkeypatch.setattr(me, "_throttle", lambda: None)
    monkeypatch.setattr(me.requests, "get", lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError, match="no rows"):
        me.fetch_grouped_daily("2026-10-23")


def test_grouped_daily_surfaces_429_as_rate_limit(monkeypatch):
    class _Resp:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("429 must be handled before raise_for_status")

        def json(self):
            return {}

    monkeypatch.setattr(me, "_throttle", lambda: None)
    monkeypatch.setattr(me.requests, "get", lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError, match="rate limited"):
        me.fetch_grouped_daily("2026-10-23")


def test_throttle_enforces_min_interval(monkeypatch):
    """Back-to-back calls must be spaced by MIN_REQUEST_INTERVAL.

    The API answers 429 otherwise; observed on consecutive grouped-daily calls.
    """
    monkeypatch.setattr(me, "_last_request_at", 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(me.time, "sleep", lambda s: sleeps.append(s))

    me._throttle()
    me._throttle()

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(me.MIN_REQUEST_INTERVAL, abs=0.01)


def test_grouped_range_calls_once_per_day(monkeypatch):
    seen: list[str] = []

    def _fake(day, *, api_key=None, session=None):
        seen.append(day)
        return pd.DataFrame({"ticker": ["AAPL"], "date": [day]})

    monkeypatch.setattr(me, "fetch_grouped_daily", _fake)
    out = me.fetch_grouped_range(["2026-10-22", "2026-10-23"])

    assert seen == ["2026-10-22", "2026-10-23"]
    assert set(out) == {"2026-10-22", "2026-10-23"}


def test_grouped_bar_timestamp():
    assert me.grouped_bar_timestamp({"t": 1792785600000}) == 1792785600000
    assert me.grouped_bar_timestamp({}) is None


def test_prior_session_is_the_session_immediately_before():
    # 2026-10-23 is a Friday; the prior session is Thursday 2026-10-22.
    assert me._prior_session("2026-10-23") == "2026-10-22"
    # 2026-10-26 is a Monday; the prior session skips the weekend.
    assert me._prior_session("2026-10-26") == "2026-10-23"


def test_build_session_frame_computes_prev_close_and_pct_change(monkeypatch):
    """prev_close and pct_chg must come from the prior session's close."""
    bars = {
        "2026-10-22": pd.DataFrame(
            {"ticker": ["AAPL"], "close": [340.0], "date": [date(2026, 10, 22)]}
        ),
        "2026-10-23": pd.DataFrame(
            {"ticker": ["AAPL"], "close": [350.0], "date": [date(2026, 10, 23)]}
        ),
    }
    monkeypatch.setattr(
        me, "fetch_grouped_daily", lambda day, **k: bars[day].copy()
    )

    frame = me.build_session_frame("2026-10-23")

    assert frame["prev_close"].iloc[0] == 340.0
    assert frame["pct_chg"].iloc[0] == pytest.approx(2.9411, abs=1e-4)
    assert frame["name"].iloc[0] is None


def test_build_session_frame_without_prior_leaves_pct_change_null(monkeypatch):
    monkeypatch.setattr(
        me,
        "fetch_grouped_daily",
        lambda day, **k: pd.DataFrame({"ticker": ["AAPL"], "close": [1.0]}),
    )

    frame = me.build_session_frame("2026-10-23", include_prior=False)

    assert "prev_close" not in frame.columns
    assert frame["pct_chg"].iloc[0] is None
