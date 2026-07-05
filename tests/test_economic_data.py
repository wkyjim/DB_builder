from __future__ import annotations

from datetime import datetime, timezone, date

import pytest

import math

from db_builder.economic_data import (
    DEFAULT_SERIES,
    EconomicSeries,
    _json_safe,
    build_derived_inflation_rows_from_base_rows,
    default_release_calendar,
    fetch_fred_csv,
    is_release_due,
    series_registry,
)


class FakeResponse:
    text = "observation_date,UNRATE\n2024-12-01,4.1\n2025-01-01,4.0\n2025-02-01,.\n"

    def raise_for_status(self):
        return None


def test_series_registry_contains_core_macro_series():
    registry = series_registry()

    for series_id in [
        "PAYEMS",
        "UNRATE",
        "GDPC1",
        "CPIAUCSL",
        "CPIAUCNS",
        "PPIFID",
        "PPIFES",
        "PCEPI",
        "PCEPILFE",
        "FEDFUNDS",
        "M2SL",
        "BAMLH0A0HYM2",
    ]:
        assert series_id in registry


def test_default_series_metadata_has_categories():
    categories = {series.category for series in DEFAULT_SERIES}

    assert {"labor", "growth", "inflation", "policy", "liquidity", "credit"} <= categories


def test_fetch_fred_csv_filters_start_date_and_missing_values(monkeypatch):
    def fake_get(url, params, timeout):
        assert params == {"id": "UNRATE"}
        return FakeResponse()

    monkeypatch.setattr("db_builder.economic_data.requests.get", fake_get)
    series = EconomicSeries("UNRATE", "Unemployment Rate", "FRED", "US", "United States", "labor", "monthly", "percent")

    rows = fetch_fred_csv(series, start_date="2025-01-01")

    assert len(rows) == 1
    assert rows[0]["date"] == date(2025, 1, 1)
    assert rows[0]["value"] == pytest.approx(4.0)
    assert rows[0]["category"] == "labor"


def test_default_release_calendar_contains_high_impact_releases():
    releases = default_release_calendar(now=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc))
    by_name = {row["release_name"]: row for row in releases}

    assert by_name["Employment Situation"]["series_ids"] == ["PAYEMS", "UNRATE", "CIVPART"]
    assert by_name["Employment Situation"]["release_datetime"] == datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc)
    assert "CPIAUCSL" in by_name["Consumer Price Index"]["series_ids"]
    assert "GDPC1" in by_name["GDP Advance Estimate and Personal Income and Outlays"]["series_ids"]


def test_release_due_gate_respects_release_time():
    release_at = datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc)

    assert not is_release_due(release_at, now=datetime(2026, 7, 2, 12, 29, tzinfo=timezone.utc))
    assert is_release_due(release_at, now=datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc))


def test_json_safe_converts_nan_to_none():
    cleaned = _json_safe({"value": math.nan, "nested": [1, math.nan]})

    assert cleaned == {"value": None, "nested": [1, None]}


def test_build_derived_inflation_rows_calculates_mom_and_yoy():
    rows = [
        {
            "series_id": "CPIAUCSL",
            "date": date(2025, month, 1),
            "value": 100 + month,
        }
        for month in range(1, 13)
    ]
    rows.append({"series_id": "CPIAUCSL", "date": date(2026, 1, 1), "value": 113.0})

    derived = build_derived_inflation_rows_from_base_rows(rows)
    by_series_date = {(row["series_id"], row["date"]): row for row in derived}

    mom = by_series_date[("DERIVED:CPI_HEADLINE_SA:MOM", date(2026, 1, 1))]
    yoy = by_series_date[("DERIVED:CPI_HEADLINE_SA:YOY", date(2026, 1, 1))]

    assert mom["value"] == pytest.approx((113.0 / 112.0 - 1) * 100)
    assert yoy["value"] == pytest.approx((113.0 / 101.0 - 1) * 100)
    assert mom["unit"] == "percent"
    assert mom["raw_payload"]["source_series_id"] == "CPIAUCSL"
