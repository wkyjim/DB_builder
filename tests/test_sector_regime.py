from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.sector_regime import build_sector_regimes, classify_cycle_phase, score_sector_trend


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def row(ticker, close=110, ma_20=104, ma_50=100, ma_200=95, rsi=55, r5=4, r20=8, r60=12):
    return {
        "ticker": ticker,
        "close": close,
        "ma_20": ma_20,
        "ma_50": ma_50,
        "ma_200": ma_200,
        "rsi_14": rsi,
        "macd_hist": 1 if close >= ma_50 else -1,
        "return_5d": r5,
        "return_20d": r20,
        "return_60d": r60,
    }


def test_emerging_bull_phase():
    assert classify_cycle_phase("uptrend", "accelerating", "outperforming", 30, [row("SMH")]) == "emerging_bull"


def test_mid_bull_phase():
    assert classify_cycle_phase("strong_uptrend", "stable_positive", "neutral", 30, [row("QQQ", r5=3, r20=6, r60=10)]) in {
        "early_bull",
        "mid_bull",
    }


def test_late_bull_phase():
    assert classify_cycle_phase("uptrend", "fading", "neutral", 40, [row("XLE", r5=-1, r20=6, r60=8, rsi=72)]) == "late_bull"


def test_correction_phase():
    assert classify_cycle_phase("uptrend", "stable_positive", "neutral", 40, [row("XLK", r5=1, r20=-2, r60=8)]) == "correction"


def test_bear_phase():
    assert classify_cycle_phase("downtrend", "negative", "underperforming", 70, [row("XLY", close=80, r5=-5, r20=-12, r60=-18)]) == "bear"


def test_missing_etf_data_neutral():
    score, state, drivers = score_sector_trend([])

    assert score == 50.0
    assert state == "neutral"
    assert "missing" in drivers[0]


def test_build_sector_regimes_outputs_sector_scores():
    market_df = pd.DataFrame(
        [
            row("SPY", r5=2, r20=4, r60=6),
            row("SMH", r5=6, r20=12, r60=18),
            row("SOXX", r5=5, r20=11, r60=17),
        ]
    )
    news_df = pd.DataFrame(
        [
            {
                "dimension_value": "Semiconductors",
                "opportunity_score": 80,
                "risk_score": 20,
                "top_themes": ["AI", "Semiconductors"],
                "top_article_ids": ["00000000-0000-0000-0000-000000000001"],
            }
        ]
    )

    signals = build_sector_regimes(market_df, news_df, window_hours=24, run_time=RUN_TIME)
    semis = next(signal for signal in signals if signal["sector_name"] == "Semiconductors")

    assert semis["sector_regime"] in {"bull", "strong_bull", "neutral"}
    assert semis["relative_strength_score"] > 50
    assert semis["top_themes"] == ["AI", "Semiconductors"]
