"""Regression: US bond-market (SIFMA) holidays are not sessions.

NYSE is OPEN on Columbus Day and Veterans Day, but the US bond market is CLOSED.
Every series that follows the bond market -- Treasury.gov par yields, the
Investing.com US Treasury yields, the ICE/CBOE Treasury indices -- publishes no
bar on those dates.

Modelling them as sessions made the historical gate read the next session's
absent prior as a storage hole and defer it forever (2026-10-13 for ^MOVE,
US10YT=X and US*YT_CMT, whose prior 2026-10-12 is Columbus Day).
"""
from __future__ import annotations

import datetime as dt
from datetime import date

import pytest

import db_builder.session_calendar as sc


# --------------------------------------------------------------------- holidays

@pytest.mark.parametrize("d", [
    date(2026, 1, 1),      # New Year
    date(2026, 1, 19),     # MLK
    date(2026, 2, 16),     # Washington
    date(2026, 4, 3),      # Good Friday
    date(2026, 5, 25),     # Memorial Day
    date(2026, 6, 19),     # Juneteenth
    date(2026, 7, 3),      # Independence Day observed (Jul 4 is a Saturday)
    date(2026, 9, 7),      # Labor Day
    date(2026, 10, 12),    # Columbus Day  <-- NYSE open, bond market closed
    date(2026, 11, 11),    # Veterans Day <-- NYSE open, bond market closed
    date(2026, 11, 26),    # Thanksgiving
    date(2026, 12, 25),    # Christmas
])
def test_bond_holidays_2026(d):
    assert sc.is_bond_market_holiday(d), f"{d} should be a bond-market holiday"


@pytest.mark.parametrize("d", [
    date(2026, 10, 13),    # the Tuesday after Columbus Day is a normal session
    date(2026, 11, 12),
    date(2026, 10, 9),
    date(2026, 12, 24),    # Christmas Eve: a full trading day, not a holiday
])
def test_non_bond_holidays_2026(d):
    assert not sc.is_bond_market_holiday(d)


def test_easter_sunday_matches_known_years():
    assert sc._easter_sunday(2025) == date(2025, 4, 20)
    assert sc._easter_sunday(2026) == date(2026, 4, 5)
    assert sc._easter_sunday(2027) == date(2027, 3, 28)


def test_new_year_saturday_is_not_observed():
    # 2023-01-01 is a Sunday -> observed Monday 2023-01-02.
    assert date(2023, 1, 2) in sc.bond_market_holidays(2023)
    # 2022-01-01 is a Saturday -> no holiday observed in 2022.
    assert date(2022, 1, 1) not in sc.bond_market_holidays(2022)
    assert date(2021, 12, 31) not in sc.bond_market_holidays(2022)


# ------------------------------------------------------------- calendar routing

def test_treasury_publication_symbols_keep_publication_confirmation():
    adapter = sc.TreasuryCalendarAdapter()
    assert adapter.requires_publication_confirmation is True
    assert adapter.name == "treasury-publication"


@pytest.mark.parametrize("symbol", ["US10YT=X", "^MOVE", "US5YT_CMT"])
def test_bond_symbols_get_the_bond_calendar(symbol):
    assert sc.adapter_for(symbol).name in ("bond-market-us", "treasury-publication")


def test_equity_vol_symbols_are_not_bond_market_symbols():
    # ^VIX / ^SKEW trade on NYSE sessions, so Columbus Day IS a session for them.
    assert "^VIX" not in sc.BOND_MARKET_SYMBOLS
    assert "^SKEW" not in sc.BOND_MARKET_SYMBOLS


def test_every_us_treasury_yield_x_symbol_is_covered():
    for t in (2, 3, 5, 7, 10, 20, 30):
        assert f"US{t}YT=X" in sc.BOND_MARKET_SYMBOLS
        assert f"US{t}YT_CMT" in sc.BOND_MARKET_SYMBOLS


def test_previous_session_skips_the_bond_holiday():
    # 2026-10-13 is a Tuesday; its previous bond-market session is Friday
    # 2026-10-09 -- Columbus Day (Monday 2026-10-12) publishes nothing.
    assert sc.previous_valid_session("US10YT=X", date(2026, 10, 13)) == date(2026, 10, 9)
    assert sc.previous_valid_session("^MOVE", date(2026, 10, 13)) == date(2026, 10, 9)
    assert sc.previous_valid_session("US10YT_CMT", date(2026, 10, 13)) == date(2026, 10, 9)


def test_previous_session_skips_a_multi_day_cluster():
    # Thanksgiving Thursday + the Friday after it are both bond holidays.
    assert sc.previous_valid_session("US10YT=X", date(2026, 11, 30)) == date(2026, 11, 25)


def test_equity_symbol_previous_session_is_unchanged():
    # ES=F keeps the plain XNYS calendar: Monday 2026-10-12 IS its prior session.
    assert sc.previous_valid_session("ES=F", date(2026, 10, 13)) == date(2026, 10, 12)


# ----------------------------------------------------------------- gate routing

def test_columbus_day_is_rejected_not_deferred():
    decision = sc.historical_eligibility("US10YT=X", date(2026, 10, 12))
    assert decision.eligible is False
    assert decision.routing == sc.ROUTE_REJECT
    assert decision.reason == sc.SessionStatus.HOLIDAY.value


def test_the_session_after_columbus_day_is_eligible():
    decision = sc.historical_eligibility(
        "US10YT=X", date(2026, 10, 13),
        known_prior_sessions={date(2026, 10, 9), date(2026, 10, 13)})
    assert decision.eligible is True
    assert decision.routing == sc.ROUTE_HISTORICAL
    assert decision.expected_prior == date(2026, 10, 9)


def test_move_after_columbus_day_is_eligible():
    decision = sc.historical_eligibility(
        "^MOVE", date(2026, 10, 13),
        known_prior_sessions={date(2026, 10, 9)})
    assert decision.eligible is True
    assert decision.expected_prior == date(2026, 10, 9)


def test_a_real_missing_prior_still_defers():
    # A hole that is NOT a holiday must still fail closed.
    decision = sc.historical_eligibility(
        "US10YT=X", date(2026, 10, 14),
        known_prior_sessions={date(2026, 10, 9)})
    assert decision.eligible is False
    assert decision.routing == sc.ROUTE_DEFERRED
    assert decision.reason == "PREVIOUS_SESSION_NOT_CONFIRMED"


def test_cmt_still_requires_official_publication():
    decision = sc.historical_eligibility(
        "US10YT_CMT", date(2026, 10, 13),
        known_prior_sessions={date(2026, 10, 9)})
    # Columbus Day is no longer the blocker; the missing official publication is.
    assert decision.reason == "TREASURY_PUBLICATION_NOT_CONFIRMED"
    assert decision.routing == sc.ROUTE_DEFERRED
