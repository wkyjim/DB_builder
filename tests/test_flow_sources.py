from datetime import date

from db_builder.flow_sources import (
    business_dates_between,
    finra_short_interest_url,
    finra_short_volume_url,
    market_session_dates_between,
    recent_business_dates,
    source_probe_dataframe,
)


def test_finra_short_volume_url_uses_expected_cdn_pattern():
    assert finra_short_volume_url(date(2026, 7, 2)) == "https://cdn.finra.org/equity/regsho/daily/CNMSshvol20260702.txt"


def test_finra_short_interest_url_uses_settlement_date():
    assert finra_short_interest_url(date(2026, 7, 15)).endswith("/shrt20260715.csv")


def test_market_session_dates_exclude_us_holiday():
    assert market_session_dates_between(date(2026, 7, 2), date(2026, 7, 6)) == [
        date(2026, 7, 2),
        date(2026, 7, 6),
    ]


def test_recent_business_dates_excludes_weekends():
    dates = recent_business_dates(3, today=date(2026, 7, 5))

    assert dates == [date(2026, 7, 3), date(2026, 7, 2), date(2026, 7, 1)]


def test_business_dates_between_excludes_weekends():
    assert business_dates_between(date(2026, 1, 1), date(2026, 1, 6)) == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]


def test_source_probe_dataframe_columns():
    df = source_probe_dataframe(
        [
            {
                "source": "CFTC COT",
                "status": "ok",
                "latest_available_date": None,
                "fetch_url": "https://example.com",
                "row_count_sample": 1,
                "parser_ready": True,
                "last_error": None,
            }
        ]
    )

    assert list(df.columns) == [
        "source",
        "status",
        "latest_available_date",
        "fetch_url",
        "row_count_sample",
        "parser_ready",
        "last_error",
    ]
