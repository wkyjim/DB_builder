from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from db_builder.secular_theme_engine import (
    SECULAR_THEMES,
    acceleration_score,
    article_theme_evidence,
    build_secular_theme_signals,
    classify_theme_phase,
    growth_score,
    keyword_matches,
    score_theme_market,
    theme_to_etfs,
)


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def article(title, days_ago, sentiment=0.5, impact=70):
    return {
        "article_id": "00000000-0000-0000-0000-000000000001",
        "title": title,
        "summary": "",
        "published_at": RUN_TIME - timedelta(days=days_ago),
        "fetched_at": RUN_TIME - timedelta(days=days_ago),
        "matched_keywords": [],
        "related_tickers": [],
        "sentiment_score": sentiment,
        "impact_score": impact,
        "confidence_score": 0.8,
        "themes": [],
        "affected_tickers": [],
    }


def market_row(ticker, close=110, ma_50=100, ma_200=95, r20=10, r60=20, rsi=55):
    return {
        "ticker": ticker,
        "close": close,
        "ma_50": ma_50,
        "ma_200": ma_200,
        "return_20d": r20,
        "return_60d": r60,
        "rsi_14": rsi,
    }


def test_theme_keyword_matching():
    matches = keyword_matches("AI infrastructure capex for data center GPU clusters", ("AI infrastructure", "GPU"))

    assert matches == ["AI infrastructure", "GPU"]


def test_theme_evidence_uses_classifier_themes_without_direct_phrase():
    theme = next(item for item in SECULAR_THEMES if item.theme_name == "AI Infrastructure")
    score, hits = article_theme_evidence(
        {
            "title": "Chip stocks rally",
            "summary": "",
            "matched_keywords": [],
            "themes": ["AI"],
        },
        theme,
    )

    assert score > 0
    assert "AI" in hits


def test_theme_to_etf_mapping():
    assert theme_to_etfs("Nuclear") == ["NLR", "XLU", "UTES"]
    assert theme_to_etfs("Cybersecurity") == ["CIBR"]


def test_growth_score_calculation():
    assert growth_score(30, 45) > 50
    assert growth_score(5, 45) < 50


def test_acceleration_score_calculation():
    assert acceleration_score(10, 20) > 50
    assert acceleration_score(1, 20) < 50


def test_high_secular_weak_tactical_is_correction():
    phase = classify_theme_phase(
        mention_count_90d=30,
        growth=80,
        acceleration=75,
        news=70,
        momentum=30,
        breadth=30,
        secular=72,
        tactical=42,
        overbought=False,
    )

    assert phase == "correction"


def test_emerging_theme_detection():
    phase = classify_theme_phase(
        mention_count_90d=3,
        growth=60,
        acceleration=80,
        news=65,
        momentum=50,
        breadth=50,
        secular=62,
        tactical=55,
        overbought=False,
    )

    assert phase == "emerging"


def test_missing_etf_data_is_neutral_not_bullish():
    theme = next(item for item in SECULAR_THEMES if item.theme_name == "Cybersecurity")
    momentum, breadth, overbought, drivers = score_theme_market(theme, pd.DataFrame())

    assert momentum == 50.0
    assert breadth == 50.0
    assert not overbought
    assert "missing" in drivers[0]


def test_build_secular_theme_signals_ranks_theme():
    articles_df = pd.DataFrame(
        [
            article("AI infrastructure data center GPU demand rises", 1),
            article("AI capex expands for data center power", 5),
            article("AI infrastructure training cluster announced", 20),
        ]
    )
    market_df = pd.DataFrame(
        [
            market_row("QQQ"),
            market_row("XLK"),
            market_row("SMH"),
            market_row("SOXX"),
        ]
    )

    signals = build_secular_theme_signals(
        articles_df,
        pd.DataFrame(),
        market_df,
        window_hours=24,
        run_time=RUN_TIME,
    )
    ai = next(signal for signal in signals if signal["theme_name"] == "AI Infrastructure")

    assert ai["mention_count_7d"] == 2
    assert ai["mention_count_30d"] == 3
    assert ai["secular_score"] > 50
    assert ai["top_subthemes"]
