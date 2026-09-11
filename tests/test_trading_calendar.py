from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from db_builder.trading_calendar import (
    coverage_is_sufficient,
    filter_valid_trading_dates,
    is_valid_nyse_session,
    latest_completed_nyse_session_date,
    reject_non_trading_dates,
    should_skip_for_latest_session,
)


def test_filter_valid_trading_dates_checks_each_date_once(monkeypatch):
    calls = []

    def fake_is_valid(value):
        calls.append(value)
        return value == date(2026, 9, 10)

    monkeypatch.setattr("db_builder.trading_calendar.is_valid_nyse_session", fake_is_valid)
    frame = pd.DataFrame(
        {
            "date": ["2026-09-10", "2026-09-10", "2026-09-12"],
            "ticker": ["A", "B", "C"],
        }
    )

    result = filter_valid_trading_dates(frame)

    assert result["ticker"].tolist() == ["A", "B"]
    assert sorted(calls) == [date(2026, 9, 10), date(2026, 9, 12)]


def test_saturday_2026_05_30_rejected():
    df = pd.DataFrame({"date": ["2026-05-30"], "ticker": ["AA"]})

    with pytest.raises(ValueError, match="Non-NYSE trading dates rejected"):
        reject_non_trading_dates(df)


def test_sunday_rejected():
    assert not is_valid_nyse_session(date(2026, 5, 31))


def test_nyse_holiday_rejected():
    assert not is_valid_nyse_session(date(2026, 1, 1))


def test_existing_latest_session_causes_skip():
    assert should_skip_for_latest_session(
        max_date=date(2026, 5, 29),
        latest_session_date=date(2026, 5, 29),
    )


def test_force_refresh_bypasses_skip():
    should_skip = should_skip_for_latest_session(
        max_date=date(2026, 5, 29),
        latest_session_date=date(2026, 5, 29),
    )
    force_refresh = True

    assert should_skip and force_refresh
    assert not (should_skip and not force_refresh)


def test_session_not_completed_until_30_minutes_after_close():
    assert latest_completed_nyse_session_date(
        datetime(2026, 6, 17, 20, 29, tzinfo=timezone.utc)
    ) == date(2026, 6, 16)

    assert latest_completed_nyse_session_date(
        datetime(2026, 6, 17, 20, 30, tzinfo=timezone.utc)
    ) == date(2026, 6, 17)


def test_complete_ticker_coverage_allows_skip():
    assert coverage_is_sufficient(13_400, 13_500)
    assert should_skip_for_latest_session(
        date(2026, 7, 8),
        date(2026, 7, 8),
        coverage_ok=True,
    )


def test_partial_ticker_coverage_prevents_skip():
    assert not coverage_is_sufficient(11_836, 13_600)
    assert not should_skip_for_latest_session(
        date(2026, 7, 8),
        date(2026, 7, 8),
        coverage_ok=False,
    )
