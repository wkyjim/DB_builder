from datetime import date, datetime

from db_builder.fred_macro import CHICAGO, expected_latest, is_business_day


def test_weekday_before_release_uses_previous_business_day():
    now = datetime(2026, 11, 6, 15, 0, tzinfo=CHICAGO)
    assert expected_latest(now) == date(2026, 11, 5)


def test_weekday_after_release_uses_same_day():
    now = datetime(2026, 11, 6, 16, 5, tzinfo=CHICAGO)
    assert expected_latest(now) == date(2026, 11, 6)


def test_weekend_uses_friday():
    now = datetime(2026, 11, 7, 12, 0, tzinfo=CHICAGO)
    assert expected_latest(now) == date(2026, 11, 6)


def test_thanksgiving_has_no_observation():
    assert not is_business_day(date(2026, 11, 26))
    now = datetime(2026, 11, 26, 16, 5, tzinfo=CHICAGO)
    assert expected_latest(now) == date(2026, 11, 25)


def test_july_fourth_observed():
    assert not is_business_day(date(2026, 7, 3))
