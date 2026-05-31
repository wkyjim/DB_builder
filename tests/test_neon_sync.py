from __future__ import annotations

import numpy as np
import pandas as pd

from db_builder.config import RAW_TABLE
from db_builder.neon_sync import clean_dataframe_for_target


def test_clean_dataframe_for_target_normalizes_dates_numbers_and_nulls():
    df = pd.DataFrame(
        {
            "date": ["2026-05-29"],
            "ticker": ["ABC"],
            "name": ["ABC Inc"],
            "market": ["105"],
            "open": ["10.5"],
            "close": [np.inf],
            "volume": ["1000"],
        }
    )

    result = clean_dataframe_for_target(df, RAW_TABLE)

    assert result["date"].iloc[0].isoformat() == "2026-05-29"
    assert result["open"].iloc[0] == 10.5
    assert pd.isna(result["close"].iloc[0])
    assert result["volume"].iloc[0] == 1000
    assert str(result["ticker"].dtype) == "string"
