from datetime import date

import pandas as pd
import pytest

from db_builder.finra_short_analytics import _rolling_percentile
from db_builder.short_snapshot import build_latest_snapshot


def test_rolling_percentile_uses_strictly_prior_history():
    values = pd.Series([1.0, 2.0, 3.0, 4.0])
    result = _rolling_percentile(values, window=3, min_periods=2)

    assert pd.isna(result.iloc[0])
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == 1.0
    assert result.iloc[3] == 1.0


def test_latest_snapshot_keeps_missing_si_null_and_builds_technical_states():
    source = pd.DataFrame(
        [
            {
                "ticker": "AAA", "name": "Alpha Inc", "existing_security_type": "common_stock",
                "analytics_date": date(2026, 8, 12), "si_settlement_date": None,
                "si_publication_date": None, "short_interest": None, "short_volume_ratio": 0.5,
                "close": 110.0, "ma20": 105.0, "ma50": 100.0, "ma100": 95.0, "ma200": 90.0,
                "rsi14": 60.0, "macd": 1.0, "macd_signal": 0.5, "macd_histogram": 0.5,
                "data_quality_status": "valid", "funding_short_quality_score": 70.0,
            }
        ]
    )

    result = build_latest_snapshot(source).iloc[0]

    assert pd.isna(result["short_interest"])
    assert result["security_type"] == "common_stock"
    assert result["trend_structure"] == "bullish_alignment"
    assert result["rsi_state"] == "strengthening"
    assert result["price_vs_ma50_pct"] == pytest.approx(10.0)
    assert len(result["source_hash"]) == 64


def test_snapshot_classifies_etf_outside_default_common_stock_universe():
    source = pd.DataFrame(
        [{
            "ticker": "ETHA", "name": "iShares Ethereum Trust ETF", "existing_security_type": None,
            "analytics_date": date(2026, 8, 12), "si_settlement_date": None,
            "si_publication_date": None, "short_volume_ratio": 0.5, "close": 30.0,
            "data_quality_status": "valid", "funding_short_quality_score": 50.0,
        }]
    )

    assert build_latest_snapshot(source).iloc[0]["security_type"] == "etf"
