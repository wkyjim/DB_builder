from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.sector_intelligence import (
    build_sector_signals,
    final_sector_score,
    score_etf_momentum,
    score_etf_trend,
    sector_market_scores,
    sector_to_etfs,
    themes_to_sectors,
)


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def test_theme_to_sector_mapping():
    assert themes_to_sectors("AI") == ["Technology", "Semiconductors"]
    assert themes_to_sectors("Geopolitics") == ["Defense", "Energy"]
    assert themes_to_sectors("Power Infrastructure") == ["Utilities", "Grid Infrastructure"]
    assert themes_to_sectors("Unknown") == []


def test_sector_to_etf_mapping():
    assert sector_to_etfs("Semiconductors") == ["SMH", "SOXX"]
    assert sector_to_etfs("Cybersecurity") == ["CIBR"]
    assert sector_to_etfs("Crypto") == ["BTC-USD", "ETH-USD"]


def test_final_sector_score_formula():
    assert final_sector_score(80, 60, 70, 20) == 53.0


def test_missing_market_data_is_neutral_not_bullish():
    momentum, trend, reasons = sector_market_scores("Semiconductors", {})

    assert momentum == 50.0
    assert trend == 50.0
    assert "missing ETF data neutralized" in reasons[0]


def test_etf_momentum_and_trend_scoring():
    row = {"return_20d": 12, "close": 110, "ma_50": 100, "ma_200": 90}

    assert score_etf_momentum(row) == 62
    assert score_etf_trend(row) == 70


def test_build_sector_signals_ranks_by_final_score():
    news_df = pd.DataFrame(
        [
            {
                "dimension_value": "AI",
                "article_count": 4,
                "avg_sentiment_score": 0.4,
                "weighted_sentiment_score": 0.5,
                "opportunity_score": 80,
                "risk_score": 20,
                "top_article_ids": ["00000000-0000-0000-0000-000000000001"],
            },
            {
                "dimension_value": "Oil",
                "article_count": 2,
                "avg_sentiment_score": -0.2,
                "weighted_sentiment_score": -0.3,
                "opportunity_score": 40,
                "risk_score": 60,
                "top_article_ids": ["00000000-0000-0000-0000-000000000002"],
            },
        ]
    )
    market_df = pd.DataFrame(
        [
            {"ticker": "QQQ", "return_20d": 8, "close": 110, "ma_50": 100, "ma_200": 90},
            {"ticker": "XLK", "return_20d": 6, "close": 110, "ma_50": 100, "ma_200": 90},
            {"ticker": "SMH", "return_20d": 10, "close": 110, "ma_50": 100, "ma_200": 90},
            {"ticker": "SOXX", "return_20d": 9, "close": 110, "ma_50": 100, "ma_200": 90},
            {"ticker": "XLE", "return_20d": -5, "close": 80, "ma_50": 90, "ma_200": 95},
        ]
    )

    signals = build_sector_signals(news_df, market_df, window_hours=24, run_time=RUN_TIME)

    assert signals[0]["rank"] == 1
    assert signals[0]["sector_name"] in {"Technology", "Semiconductors"}
    assert signals[0]["final_score"] > signals[-1]["final_score"]
    assert signals[0]["top_themes"]
