from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.recalculate_core_ytd import CSV_COLUMNS, validate_ytd_csv


def test_validate_ytd_csv_accepts_unique_finite_rows(tmp_path: Path):
    path = tmp_path / "ytd.csv"
    pd.DataFrame(
        [
            {
                "date": "2026-01-02",
                "ticker": "AAA",
                "ytd_pct_chg": 1.5,
                "base_date": "2025-12-31",
                "base_close": 10.0,
                "base_method": "prior_year_close",
            }
        ],
        columns=CSV_COLUMNS,
    ).to_csv(path, index=False)

    summary = validate_ytd_csv(path)

    assert summary["rows"] == 1
    assert summary["tickers"] == 1


def test_validate_ytd_csv_rejects_duplicate_keys(tmp_path: Path):
    path = tmp_path / "ytd.csv"
    row = {
        "date": "2026-01-02",
        "ticker": "AAA",
        "ytd_pct_chg": 1.5,
        "base_date": "2025-12-31",
        "base_close": 10.0,
        "base_method": "prior_year_close",
    }
    pd.DataFrame([row, row], columns=CSV_COLUMNS).to_csv(path, index=False)

    try:
        validate_ytd_csv(path)
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
    else:
        raise AssertionError("Expected duplicate-key validation failure.")
