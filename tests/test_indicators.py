from __future__ import annotations

import pandas as pd

from db_builder.indicators import INDICATOR_COLUMNS, calculate_group


def test_calculate_group_returns_expected_indicator_columns():
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=260, freq="D"),
            "ticker": ["ABC"] * 260,
            "open": range(260),
            "high": [x + 2 for x in range(260)],
            "low": [x for x in range(260)],
            "close": [x + 1 for x in range(260)],
            "volume": [1000 + x for x in range(260)],
        }
    )

    result = calculate_group(df)

    assert result.columns.tolist() == ["date", "ticker"] + INDICATOR_COLUMNS
    assert len(result) == 260
    assert result["ticker"].nunique() == 1
    assert result["ticker"].iloc[0] == "ABC"
    assert pd.notna(result["ma_200"].iloc[-1])
    assert pd.notna(result["high_52w"].iloc[-1])

