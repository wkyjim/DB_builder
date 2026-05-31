from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from db_builder.trading_calendar import (
    is_valid_nyse_session,
    reject_non_trading_dates,
    should_skip_for_latest_session,
)


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

