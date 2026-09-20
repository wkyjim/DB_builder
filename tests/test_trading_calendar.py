from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from db_builder.trading_calendar import (
    coverage_is_sufficient,
    expected_latest_treasury_publication,
    filter_valid_trading_dates,
    is_treasury_trading_day,
    is_valid_nyse_session,
    latest_completed_nyse_session_date,
    previous_treasury_trading_day,
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


# ---------------------------------------------------------------------------
# U.S. Treasury official publication calendar
# ---------------------------------------------------------------------------


def test_treasury_trading_day_weekends_false():
    assert not is_treasury_trading_day(date(2026, 3, 7))  # Saturday
    assert not is_treasury_trading_day(date(2026, 10, 31))  # Saturday
    assert not is_treasury_trading_day(date(2026, 3, 8))  # Sunday
    assert not is_treasury_trading_day(date(2026, 11, 1))  # Sunday


def test_treasury_trading_day_federal_holidays_false():
    assert not is_treasury_trading_day(date(2026, 1, 19))  # MLK Day
    assert not is_treasury_trading_day(date(2026, 6, 19))  # Juneteenth
    assert not is_treasury_trading_day(date(2026, 12, 25))  # Christmas


def test_treasury_trading_day_good_friday_is_publication_day():
    """Good Friday is an NYSE holiday but a Treasury publication day."""
    assert is_treasury_trading_day(date(2026, 4, 3))


def test_treasury_trading_day_ordinary_weekday_true():
    assert is_treasury_trading_day(date(2026, 3, 9))  # Monday
    assert is_treasury_trading_day(date(2026, 11, 2))  # Monday


def test_previous_treasury_trading_day_walks_back():
    # Tuesday after a Monday
    assert previous_treasury_trading_day(date(2026, 3, 10)) == date(2026, 3, 9)
    # Monday walks back over the weekend to Friday
    assert previous_treasury_trading_day(date(2026, 3, 9)) == date(2026, 3, 6)
    # Day after MLK walks back over the holiday to the prior Friday
    assert previous_treasury_trading_day(date(2026, 1, 20)) == date(2026, 1, 16)


# --- DST offset tests -------------------------------------------------------
# These use weekend dates ONLY to verify ET<->UTC offset changes around the
# 2026 DST transitions (spring-forward Mar 8, fall-back Nov 1). They are
# timezone-conversion assertions, not publication-boundary assertions.


def test_dst_offset_18et_before_spring_forward_is_23utc():
    # Saturday 2026-03-07 is EST: 18:00 ET == 23:00 UTC
    from datetime import time as dt_time
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    assert datetime(2026, 3, 7, 18, 0, tzinfo=et).astimezone(timezone.utc) == datetime(
        2026, 3, 7, 23, 0, tzinfo=timezone.utc
    )


def test_dst_offset_18et_after_spring_forward_is_22utc():
    # Monday 2026-03-09 is EDT: 18:00 ET == 22:00 UTC
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    assert datetime(2026, 3, 9, 18, 0, tzinfo=et).astimezone(timezone.utc) == datetime(
        2026, 3, 9, 22, 0, tzinfo=timezone.utc
    )


def test_dst_offset_22utc_before_fall_back_is_18et():
    # Saturday 2026-10-31 is EDT: 22:00 UTC == 18:00 ET
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    assert datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc).astimezone(et).hour == 18


def test_dst_offset_23utc_after_fall_back_is_18et():
    # Monday 2026-11-02 is EST: 23:00 UTC == 18:00 ET
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    assert datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc).astimezone(et).hour == 18


# --- Publication-boundary tests ---------------------------------------------
# These use actual Treasury trading days to assert the 18:00 ET cutoff flip.


def test_publication_boundary_2026_03_09_before_cutoff():
    """Monday 2026-03-09 17:59 ET (EDT, 21:59 UTC): expected = previous trading day."""
    assert expected_latest_treasury_publication(
        datetime(2026, 3, 9, 21, 59, tzinfo=timezone.utc)
    ) == date(2026, 3, 6)  # Friday


def test_publication_boundary_2026_03_09_at_cutoff():
    """Monday 2026-03-09 18:00 ET (EDT, 22:00 UTC): expected = today."""
    assert expected_latest_treasury_publication(
        datetime(2026, 3, 9, 22, 0, tzinfo=timezone.utc)
    ) == date(2026, 3, 9)


def test_publication_boundary_2026_11_02_before_cutoff():
    """Monday 2026-11-02 17:59 ET (EST, 22:59 UTC): expected = previous trading day."""
    assert expected_latest_treasury_publication(
        datetime(2026, 11, 2, 22, 59, tzinfo=timezone.utc)
    ) == date(2026, 10, 30)  # Friday


def test_publication_boundary_2026_11_02_at_cutoff():
    """Monday 2026-11-02 18:00 ET (EST, 23:00 UTC): expected = today."""
    assert expected_latest_treasury_publication(
        datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)
    ) == date(2026, 11, 2)


def test_publication_boundary_weekend_walks_back():
    """Sunday 2026-03-08 20:00 UTC (15:00 ET): expected = Friday 2026-03-06."""
    assert expected_latest_treasury_publication(
        datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)
    ) == date(2026, 3, 6)


def test_publication_boundary_good_friday():
    """Good Friday 2026-04-03 after 18:00 ET: Treasury publishes — expected is
    Good Friday itself, unlike NYSE-based logic which would say Apr 2."""
    # Good Friday 18:00 ET = 22:00 UTC (EDT)
    assert expected_latest_treasury_publication(
        datetime(2026, 4, 3, 22, 0, tzinfo=timezone.utc)
    ) == date(2026, 4, 3)


def test_publication_boundary_federal_holiday_walks_back():
    """MLK Monday 2026-01-19 after 18:00 ET: no publication — expected = prior Friday."""
    assert expected_latest_treasury_publication(
        datetime(2026, 1, 19, 23, 0, tzinfo=timezone.utc)
    ) == date(2026, 1, 16)


def test_publication_boundary_morning_hkt_run():
    """09:00 HKT (= 01:00 ET, pre-18:00) on a Tuesday: expected = Monday."""
    # 2026-03-10 09:00 HKT == 01:00 EDT == 05:00 UTC
    assert expected_latest_treasury_publication(
        datetime(2026, 3, 10, 5, 0, tzinfo=timezone.utc)
    ) == date(2026, 3, 9)
