from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.indicator_staged_backfill import (
    CSV_READ_OPTIONS,
    calculate_indicator_csv,
    csv_tickers,
    latest_raw_date,
    load_and_validate_indicator_csv,
)


def test_calculate_indicator_csv_writes_only_target_date(tmp_path):
    raw_path = tmp_path / "raw.csv"
    output_path = tmp_path / "indicators.csv"
    dates = pd.bdate_range("2025-04-01", periods=330)
    rows = []
    for ticker in ["AAA", "BBB", "NA", "NAN"]:
        for index, date in enumerate(dates):
            rows.append(
                {
                    "date": date,
                    "ticker": ticker,
                    "open": 100 + index,
                    "high": 102 + index,
                    "low": 99 + index,
                    "close": 101 + index,
                    "volume": 1000 + index,
                }
            )
    pd.DataFrame(rows).to_csv(raw_path, index=False)

    count = calculate_indicator_csv(
        raw_path,
        output_path,
        target_date=dates[-1].date().isoformat(),
        chunk_rows=400,
    )

    result = pd.read_csv(output_path, **CSV_READ_OPTIONS)
    assert count == 4
    assert set(result["ticker"]) == {"AAA", "BBB", "NA", "NAN"}
    assert set(result["date"]) == {dates[-1].date().isoformat()}


def test_validate_indicator_csv_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "duplicate.csv"
    row = {"date": "2026-07-08", "ticker": "AAA"}
    for column in __import__("db_builder.indicators", fromlist=["INDICATOR_COLUMNS"]).INDICATOR_COLUMNS:
        row[column] = 1.0
    pd.DataFrame([row, row]).to_csv(path, index=False)

    try:
        load_and_validate_indicator_csv(path, "2026-07-08")
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("Expected duplicate validation failure")


def test_latest_raw_date_uses_database_maximum():
    class Result:
        def scalar(self):
            return pd.Timestamp("2026-07-08").date()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query):
            return Result()

    engine = SimpleNamespace(connect=lambda: Connection())

    assert latest_raw_date(engine) == "2026-07-08"


def test_csv_tickers_reads_all_chunks(tmp_path):
    path = tmp_path / "raw.csv"
    pd.DataFrame({"ticker": ["AAA", "BBB", "AAA", "CCC"]}).to_csv(path, index=False)

    assert csv_tickers(path, chunk_rows=2) == {"AAA", "BBB", "CCC"}


def test_daily_runner_uses_fetch_only_and_staged_pipeline():
    root = Path(__file__).resolve().parents[1]
    content = (root / "scripts" / "auto_postgreSQL_db.bat").read_text(encoding="utf-8")

    assert "pgSQL_equities_auto.py' 'pgSQL_equities_auto.py' @('--fetch-only') 1200" in content
    assert "indicator_staged_backfill.py" in content
    assert "@('--tables', 'equities')" in content
    assert "Invoke-IndicatorBatches" not in content
    assert content.index("pgSQL_equities_auto.py' 'pgSQL_equities_auto.py'") < content.index(
        "pgSQL_daily_bulk_sync_to_neon.py' 'pgSQL_daily_bulk_sync_to_neon.py'"
    )
    assert content.index(
        "pgSQL_daily_bulk_sync_to_neon.py' 'pgSQL_daily_bulk_sync_to_neon.py'"
    ) < content.index("indicator_staged_backfill.py' 'indicator_staged_backfill.py'")
