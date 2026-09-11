from datetime import date

import pandas as pd

from db_builder.neon_sync import earliest_count_mismatch, fetch_local_rows_for_daily_sync


def test_earliest_count_mismatch_finds_missing_neon_session():
    local = pd.DataFrame(
        {"date": [date(2026, 8, 13), date(2026, 8, 14)], "row_count": [100, 101]}
    )
    neon = pd.DataFrame({"date": [date(2026, 8, 13)], "row_count": [100]})

    assert earliest_count_mismatch(local, neon) == date(2026, 8, 14)


def test_earliest_count_mismatch_returns_none_when_counts_match():
    counts = pd.DataFrame({"date": [date(2026, 8, 13)], "row_count": [100]})

    assert earliest_count_mismatch(counts, counts.copy()) is None


def test_earliest_count_mismatch_finds_neon_only_session():
    local = pd.DataFrame({"date": [date(2026, 8, 13)], "row_count": [100]})
    neon = pd.DataFrame(
        {"date": [date(2026, 8, 13), date(2026, 8, 14)], "row_count": [100, 1]}
    )

    assert earliest_count_mismatch(local, neon) == date(2026, 8, 14)


def test_daily_sync_query_respects_minimum_retention_date(monkeypatch):
    captured = {}

    def fake_read_sql(sql, engine, params):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr("db_builder.neon_sync.pd.read_sql", fake_read_sql)

    fetch_local_rows_for_daily_sync(
        object(),
        "public.us_equities",
        date(2026, 5, 1),
        minimum_date=date(2026, 5, 1),
    )

    assert "date >= %(minimum_date)s" in captured["sql"]
    assert captured["params"]["minimum_date"] == date(2026, 5, 1)
