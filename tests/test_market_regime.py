from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.market_regime import (
    build_market_regime_signal,
    classify_regime,
    score_macro_regime,
    score_news_regime,
)


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def test_classify_regime_labels_clear_risk_on_and_risk_off():
    assert classify_regime(100, 40)[0] == "risk_on"
    assert classify_regime(20, 80)[0] == "risk_off"
    assert classify_regime(50, 45)[0] == "mixed"
    assert classify_regime(0, 0) == ("neutral", 0.0)


def test_score_news_regime_uses_canonical_theme_signals():
    news = pd.DataFrame(
        [
            {
                "dimension_type": "theme",
                "dimension_value": "Market Sentiment",
                "article_count": 2,
                "opportunity_score": 70,
                "risk_score": 10,
            },
            {
                "dimension_type": "theme",
                "dimension_value": "loose label",
                "article_count": 1,
                "opportunity_score": 100,
                "risk_score": 100,
            },
        ]
    )

    risk_on, risk_off, drivers = score_news_regime(news)

    assert risk_on > risk_off
    assert drivers[0].startswith("news:Market Sentiment")


def test_score_macro_regime_flags_vix_jump_as_risk_off():
    macro = pd.DataFrame(
        [
            {"symbol": "^VIX", "asset_type": "stock_index", "pct_chg": 4.0},
            {"symbol": "^GSPC", "asset_type": "stock_index", "pct_chg": -1.5},
        ]
    )

    risk_on, risk_off, drivers = score_macro_regime(macro)

    assert risk_off > risk_on
    assert any("^VIX" in driver for driver in drivers)


def test_build_market_regime_signal_combines_news_and_macro():
    news = pd.DataFrame(
        [
            {
                "dimension_type": "theme",
                "dimension_value": "AI",
                "article_count": 2,
                "opportunity_score": 60,
                "risk_score": 10,
            }
        ]
    )
    macro = pd.DataFrame(
        [
            {"symbol": "NQ=F", "asset_type": "futures", "pct_chg": 1.0},
        ]
    )

    signal = build_market_regime_signal(news, macro, window_hours=24, run_time=RUN_TIME)

    assert signal["window_hours"] == 24
    assert signal["regime_label"] == "risk_on"
    assert signal["risk_on_score"] > signal["risk_off_score"]
    assert signal["news_signal_count"] == 1
    assert signal["macro_signal_count"] == 1
