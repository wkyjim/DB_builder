from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from db_builder.opportunity_scanner import (
    build_opportunity_signals,
    classify_opportunity,
    exclusion_reasons,
    technical_score,
)


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def test_technical_score_rewards_constructive_momentum():
    score, reasons = technical_score(
        {
            "rsi_14": 55,
            "macd_hist": 0.5,
            "return_5d": 4,
            "return_20d": 10,
            "volume_ratio_20": 1.8,
        }
    )

    assert score > 25
    assert "positive MACD histogram" in reasons
    assert any("elevated volume" in reason for reason in reasons)


def test_technical_score_penalizes_weak_or_overbought_setups():
    score, reasons = technical_score(
        {
            "rsi_14": 82,
            "macd_hist": -0.2,
            "return_5d": -5,
            "return_20d": -12,
            "volume_ratio_20": 1.0,
        }
    )

    assert score < 0
    assert any("overbought RSI" in reason for reason in reasons)


def test_technical_score_is_neutral_when_data_is_missing():
    score, reasons = technical_score({})

    assert score == 0.0
    assert reasons == ["no local technical data"]


def test_classify_opportunity_uses_score_spread():
    assert classify_opportunity(80, 20) == "opportunity"
    assert classify_opportunity(20, 60) == "risk"
    assert classify_opportunity(35, 30) == "watchlist"


def test_build_opportunity_signals_combines_news_and_indicators():
    news = pd.DataFrame(
        [
            {
                "ticker": "NVDA",
                "article_count": 2,
                "high_impact_count": 1,
                "weighted_sentiment_score": 0.7,
                "opportunity_score": 60,
                "risk_score": 10,
            }
        ]
    )
    indicators = pd.DataFrame(
        [
            {
                "ticker": "NVDA",
                "date": date(2026, 6, 5),
                "close": 120,
                "mkt_cap": 1_000_000_000,
                "indicator_date": date(2026, 6, 5),
                "ma_50": 110,
                "ma_200": 100,
                "rsi_14": 55,
                "macd_hist": 0.4,
                "return_5d": 3,
                "return_20d": 8,
                "volume_ratio_20": 1.6,
            }
        ]
    )

    signals = build_opportunity_signals(
        news,
        indicators,
        window_hours=24,
        run_time=RUN_TIME,
        universe="all",
        include_meme=True,
    )

    assert len(signals) == 1
    assert signals[0]["ticker"] == "NVDA"
    assert signals[0]["signal_label"] == "opportunity"
    assert signals[0]["opportunity_score"] > signals[0]["news_opportunity_score"]
    assert signals[0]["latest_date"] == date(2026, 6, 5)


def test_empty_news_returns_no_opportunities():
    assert build_opportunity_signals(pd.DataFrame(), pd.DataFrame(), window_hours=24, run_time=RUN_TIME) == []


def test_gme_excluded_by_default():
    news = pd.DataFrame(
        [{"ticker": "GME", "article_count": 1, "opportunity_score": 80, "risk_score": 5}]
    )
    indicators = pd.DataFrame(
        [
            {
                "ticker": "GME",
                "date": date(2026, 6, 5),
                "close": 30,
                "mkt_cap": 10_000_000_000,
                "indicator_date": date(2026, 6, 5),
                "rsi_14": 50,
            }
        ]
    )

    assert build_opportunity_signals(news, indicators, window_hours=24, run_time=RUN_TIME) == []
    assert "excluded meme/high-noise ticker" in exclusion_reasons("GME", indicators.iloc[0].to_dict())


def test_gme_included_only_with_all_universe_and_include_meme():
    news = pd.DataFrame(
        [{"ticker": "GME", "article_count": 1, "opportunity_score": 80, "risk_score": 5}]
    )
    indicators = pd.DataFrame(
        [
            {
                "ticker": "GME",
                "date": date(2026, 6, 5),
                "close": 30,
                "mkt_cap": 10_000_000_000,
                "indicator_date": date(2026, 6, 5),
                "rsi_14": 50,
            }
        ]
    )

    assert build_opportunity_signals(
        news,
        indicators,
        window_hours=24,
        run_time=RUN_TIME,
        universe="all",
    ) == []
    signals = build_opportunity_signals(
        news,
        indicators,
        window_hours=24,
        run_time=RUN_TIME,
        universe="all",
        include_meme=True,
    )

    assert len(signals) == 1
    assert signals[0]["ticker"] == "GME"


def test_watchlist_universe_excludes_non_watchlist_single_names():
    news = pd.DataFrame(
        [{"ticker": "NVDA", "article_count": 1, "opportunity_score": 60, "risk_score": 10}]
    )
    indicators = pd.DataFrame(
        [
            {
                "ticker": "NVDA",
                "date": date(2026, 6, 5),
                "close": 120,
                "mkt_cap": 1_000_000_000,
                "indicator_date": date(2026, 6, 5),
                "rsi_14": 55,
            }
        ]
    )

    signals = build_opportunity_signals(news, indicators, window_hours=24, run_time=RUN_TIME)

    assert signals == []
    assert "excluded by watchlist universe" in exclusion_reasons("NVDA", indicators.iloc[0].to_dict())


def test_missing_indicators_are_neutral_for_approved_watchlist_tickers():
    news = pd.DataFrame(
        [{"ticker": "SMH", "article_count": 1, "opportunity_score": 40, "risk_score": 10}]
    )
    indicators = pd.DataFrame(
        [
            {
                "ticker": "SMH",
                "date": date(2026, 6, 5),
                "close": 250,
                "mkt_cap": None,
                "indicator_date": None,
                "rsi_14": None,
            }
        ]
    )

    signals = build_opportunity_signals(news, indicators, window_hours=24, run_time=RUN_TIME)

    assert len(signals) == 1
    assert signals[0]["technical_score"] == 0.0
    assert signals[0]["opportunity_score"] == signals[0]["news_opportunity_score"]
    assert "no local technical indicators" in signals[0]["reasons"]


def test_overbought_rsi_reduces_opportunity_score():
    news = pd.DataFrame(
        [{"ticker": "SMH", "article_count": 1, "opportunity_score": 60, "risk_score": 10}]
    )
    indicators = pd.DataFrame(
        [
            {
                "ticker": "SMH",
                "date": date(2026, 6, 5),
                "close": 250,
                "mkt_cap": None,
                "indicator_date": date(2026, 6, 5),
                "ma_50": 260,
                "ma_200": 270,
                "rsi_14": 82,
                "macd_hist": 0,
                "return_5d": 0,
                "return_20d": 0,
                "volume_ratio_20": 1,
            }
        ]
    )

    signals = build_opportunity_signals(news, indicators, window_hours=24, run_time=RUN_TIME)

    assert len(signals) == 1
    assert signals[0]["opportunity_score"] < signals[0]["news_opportunity_score"]
    assert signals[0]["risk_score"] > signals[0]["news_risk_score"]
    assert any("overbought RSI" in reason for reason in signals[0]["reasons"])
    assert any("close below ma_50" in reason for reason in signals[0]["reasons"])
