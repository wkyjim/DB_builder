from datetime import date

import pandas as pd
import pytest

import db_builder.finra_short_interest as short_interest_module
from db_builder.finra_short_interest import (
    ShortInterestFetchResult,
    compute_short_interest_features,
    discover_short_interest_settlement_dates,
    fetch_massive_short_interest,
    massive_fallback_is_due,
    parse_massive_short_interest_payload,
    parse_finra_short_interest_text,
    parse_reporting_calendar_tables,
)


HEADER = (
    "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|marketClassCode|"
    "currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|averageDailyVolumeQuantity|"
    "daysToCoverQuantity|revisionFlag|changePercent|changePreviousNumber|settlementDate"
)


def test_settlement_date_discovery_uses_market_sessions():
    dates = discover_short_interest_settlement_dates(date(2026, 7, 1), date(2026, 7, 31))
    assert dates == [date(2026, 7, 15), date(2026, 7, 31)]


def test_reporting_calendar_parser_preserves_publication_date():
    table = pd.DataFrame(
        {
            "Settlement Date": ["January 15 (Thursday)"],
            "Due Date": ["January 20 (Tuesday)"],
            "Publication Date": ["January 27 (Tuesday)"],
        }
    )
    # A complete table is identified as current year.
    table = pd.concat([table] * 24, ignore_index=True)
    mapping = parse_reporting_calendar_tables([table], current_year=2026)
    assert mapping[date(2026, 1, 15)] == date(2026, 1, 27)


def test_reporting_calendar_maps_july_31_to_august_11():
    rows = [{
        "Settlement Date": "July 31 (Friday)",
        "Due Date": "August 4 (Tuesday)",
        "Publication Date": "August 11 (Tuesday)",
    }]
    rows.extend(
        {
            "Settlement Date": f"January {index + 1}",
            "Due Date": f"January {index + 2}",
            "Publication Date": f"January {index + 3}",
        }
        for index in range(23)
    )
    mapping = parse_reporting_calendar_tables([pd.DataFrame(rows)], current_year=2026)
    assert mapping[date(2026, 7, 31)] == date(2026, 8, 11)


def test_massive_fallback_requires_publication_and_missing_local_data():
    settlement = date(2026, 7, 31)
    publication = date(2026, 8, 11)
    assert not massive_fallback_is_due(settlement, publication, as_of_date=date(2026, 8, 10), data_already_present=False)
    assert massive_fallback_is_due(settlement, publication, as_of_date=publication, data_already_present=False)
    assert not massive_fallback_is_due(settlement, publication, as_of_date=publication, data_already_present=True)


def test_massive_payload_normalizes_to_existing_finra_schema():
    payload = {
        "status": "OK",
        "results": [{
            "ticker": "AMD",
            "settlement_date": "2026-07-31",
            "short_interest": 1200,
            "avg_daily_volume": 400,
            "days_to_cover": 3.0,
        }],
    }
    rows = parse_massive_short_interest_payload(
        payload,
        settlement_date=date(2026, 7, 31),
        publication_date=date(2026, 8, 11),
        publication_date_source="finra_calendar",
        previous_short_interest={"AMD": 1000},
        minimum_rows=1,
    )
    assert rows[0]["ticker"] == "AMD"
    assert rows[0]["previous_short_position_quantity"] == 1000
    assert rows[0]["change_previous_number"] == 200
    assert rows[0]["change_percent"] == pytest.approx(20.0)
    assert rows[0]["data_quality_status"] == "valid_massive_fallback"
    assert "apiKey" not in rows[0]["source_file"]


def test_massive_fetch_uses_one_bulk_call_and_rejects_secret_leakage():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "OK",
                "results": [{
                    "ticker": "AAPL",
                    "settlement_date": "2026-07-31",
                    "short_interest": 500,
                    "avg_daily_volume": 250,
                    "days_to_cover": 2,
                }],
            }

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params, timeout):
            self.calls.append((url, params, timeout))
            return Response()

    session = Session()
    rows = fetch_massive_short_interest(
        date(2026, 7, 31),
        publication_date=date(2026, 8, 11),
        publication_date_source="finra_calendar",
        api_key="private-test-key",
        session=session,
        minimum_rows=1,
    )
    assert len(session.calls) == 1
    assert session.calls[0][1] == {
        "settlement_date": "2026-07-31",
        "limit": 50000,
        "sort": "ticker.asc",
        "apiKey": "private-test-key",
    }
    assert rows[0]["ticker"] == "AAPL"


def test_massive_http_error_does_not_expose_api_key():
    class Response:
        status_code = 403

    class Session:
        @staticmethod
        def get(url, *, params, timeout):
            return Response()

    with pytest.raises(RuntimeError) as exc:
        fetch_massive_short_interest(
            date(2026, 7, 31),
            publication_date=date(2026, 8, 11),
            publication_date_source="finra_calendar",
            api_key="private-test-key",
            session=Session(),
            minimum_rows=1,
        )
    assert "private-test-key" not in str(exc.value)
    assert "HTTP 403" in str(exc.value)


def test_run_fetch_falls_back_only_after_finra_is_missing(monkeypatch):
    settlement = date(2026, 7, 31)
    publication = date(2026, 8, 11)
    fallback_calls = []
    fallback_row = {
        "settlement_date": settlement,
        "publication_date": publication,
        "publication_date_source": "finra_calendar",
        "ticker": "AMD",
        "current_short_position_quantity": 1200.0,
        "source_file": "massive:stocks/v1/short-interest?settlement_date=2026-07-31",
    }
    monkeypatch.setattr(short_interest_module, "setup_short_analytics_schema", lambda engine: None)
    monkeypatch.setattr(short_interest_module, "_existing_data_dates", lambda engine, dates: set())
    monkeypatch.setattr(short_interest_module, "_existing_success_dates", lambda engine, dates: set())
    monkeypatch.setattr(short_interest_module, "fetch_reporting_calendar", lambda timeout=30: {settlement: publication})
    monkeypatch.setattr(short_interest_module, "http_session", lambda: object())
    monkeypatch.setattr(
        short_interest_module,
        "fetch_finra_short_interest_files",
        lambda *args, **kwargs: ShortInterestFetchResult(missing_dates=[settlement]),
    )
    monkeypatch.setattr(short_interest_module, "_previous_short_interest_map", lambda engine, source_date: {})

    def fake_massive(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        return [fallback_row]

    monkeypatch.setattr(short_interest_module, "fetch_massive_short_interest", fake_massive)
    monkeypatch.setattr(short_interest_module, "record_flow_source_health", lambda *args, **kwargs: None)

    result = short_interest_module.run_finra_short_interest_fetch(
        object(),
        settlement_dates=[settlement],
        dry_run=True,
        massive_api_key="private-test-key",
        as_of_date=publication,
        progress=None,
    )
    assert len(fallback_calls) == 1
    assert result["successful_dates"] == [settlement]
    assert result["fallback_dates"] == [settlement]
    assert result["fallback_rows"] == 1
    assert result["missing_dates"] == []


def test_short_interest_parser_preserves_raw_fields():
    body = HEADER + "\n20260715|AAPL|Apple Inc.|Q|NASDAQ|1000|900||500|2.0|R|11.11|100|2026-07-15\n"
    row = parse_finra_short_interest_text(
        body,
        minimum_rows=1,
        source_file="shrt20260715.csv",
        publication_date=date(2026, 7, 24),
        publication_date_source="finra_calendar",
    )[0]

    assert row["current_short_position_quantity"] == 1000
    assert row["revision_flag"] == "R"
    assert row["settlement_date"] == date(2026, 7, 15)


def test_short_interest_parser_rejects_malformed_input():
    with pytest.raises(ValueError, match="Missing FINRA short-interest columns"):
        parse_finra_short_interest_text("settlementDate|symbolCode\n2026-07-15|AAPL\n", minimum_rows=1)


def test_si_slope_r2_and_persistence_for_clean_growth():
    dates = pd.date_range("2024-01-15", periods=50, freq="SMS")
    frame = pd.DataFrame(
        {
            "settlement_date": dates,
            "publication_date": dates + pd.Timedelta(days=10),
            "ticker": "AAA",
            "current_short_position_quantity": [1000 * (1.02**index) for index in range(50)],
            "days_to_cover_quantity": 2,
            "stock_split_flag": None,
        }
    )
    result = compute_short_interest_features(frame)
    last = result.iloc[-1]

    assert last["si_slope_12m"] > 0
    assert last["si_r2_12m"] > 0.99
    assert last["own_si_percentile_1y"] == 1.0


def test_stock_split_flags_naive_change():
    frame = pd.DataFrame(
        {
            "settlement_date": [date(2026, 1, 15), date(2026, 1, 30)],
            "publication_date": [date(2026, 1, 27), date(2026, 2, 10)],
            "ticker": ["AAA", "AAA"],
            "current_short_position_quantity": [1000, 2000],
            "days_to_cover_quantity": [2, 2],
            "stock_split_flag": [None, "Y"],
        }
    )
    result = compute_short_interest_features(frame)

    assert bool(result.iloc[-1]["corporate_action_flag"]) is True
    assert result.iloc[-1]["si_change_1obs"] is None
