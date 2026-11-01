from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from db_builder.market_regime_v2 import (
    REGIME_FACTOR_WEIGHTS,
    build_market_regime_v2,
    classify_market_phase,
    factor_availability,
    weighted_regime_score,
)


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def market_rows(multiplier: float = 1.0, close_above_ma: bool = True) -> pd.DataFrame:
    rows = []
    for ticker in ["SPY", "QQQ", "IWM", "SMH", "XLK", "XLF", "XLV", "XLE"]:
        close = 110 if close_above_ma else 80
        rows.append(
            {
                "ticker": ticker,
                "close": close,
                "ma_20": 100,
                "ma_50": 98,
                "ma_200": 95,
                "rsi_14": 58 if close_above_ma else 32,
                "macd_hist": 1 if close_above_ma else -1,
                "return_5d": 4 * multiplier,
                "return_20d": 8 * multiplier,
                "return_60d": 12 * multiplier,
            }
        )
    return pd.DataFrame(rows)


def macro_rows(vix_close=14, vix_chg=-2, equity_chg=1.5) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": "^VIX", "close": vix_close, "pct_chg": vix_chg},
            {"symbol": "^GSPC", "close": 6000, "pct_chg": equity_chg},
            {"symbol": "^IXIC", "close": 19000, "pct_chg": equity_chg},
            {"symbol": "NQ=F", "close": 20000, "pct_chg": equity_chg},
            {"symbol": "ES=F", "close": 6000, "pct_chg": equity_chg},
            {"symbol": "RTY=F", "close": 2200, "pct_chg": equity_chg},
            {"symbol": "BTC-USD", "close": 100000, "pct_chg": 3 if equity_chg > 0 else -5},
            {"symbol": "ETH-USD", "close": 4000, "pct_chg": 2 if equity_chg > 0 else -4},
            {"symbol": "GC=F", "close": 2400, "pct_chg": -1 if equity_chg > 0 else 3},
            {"symbol": "^TNX", "close": 4.2, "pct_chg": -1},
            {"symbol": "^TYX", "close": 4.5, "pct_chg": -1},
        ]
    )


def test_strong_risk_on_case():
    signal = build_market_regime_v2(
        market_rows(),
        macro_rows(),
        pd.DataFrame([{"dimension_type": "theme", "dimension_value": "AI", "opportunity_score": 80, "risk_score": 10}]),
        window_hours=24,
        run_time=RUN_TIME,
    )

    assert signal["market_regime"] in {"risk_on", "strong_risk_on"}
    assert signal["trend_state"] in {"uptrend", "strong_uptrend"}
    assert signal["risk_appetite_state"] == "risk_seeking"


def test_strong_risk_off_case():
    signal = build_market_regime_v2(
        market_rows(multiplier=-1, close_above_ma=False),
        macro_rows(vix_close=34, vix_chg=15, equity_chg=-2.5),
        pd.DataFrame([{"dimension_type": "theme", "dimension_value": "Geopolitics", "opportunity_score": 5, "risk_score": 90}]),
        window_hours=24,
        run_time=RUN_TIME,
    )

    assert signal["market_regime"] in {"risk_off", "strong_risk_off"}
    assert signal["volatility_state"] == "stressed"
    assert signal["risk_appetite_state"] in {"risk_reducing", "risk_aversion"}


def news_rows(opp=80, risk=10):
    return pd.DataFrame([{"dimension_type": "theme", "dimension_value": "AI",
                          "opportunity_score": opp, "risk_score": risk}])


def test_weighted_score_reproduces_fixed_weights_when_all_feeds_present():
    scores = {k: 50.0 for k in REGIME_FACTOR_WEIGHTS}
    scores.update({"technical": 70.0, "news": 30.0})
    value, info = weighted_regime_score(scores)
    expected = sum(scores[k] * REGIME_FACTOR_WEIGHTS[k] for k in REGIME_FACTOR_WEIGHTS)
    assert value == round(expected, 4)
    assert info["dropped"] == []
    assert set(info["weights"]) == set(REGIME_FACTOR_WEIGHTS)


def test_weighted_score_renormalizes_when_news_missing():
    scores = {k: 50.0 for k in REGIME_FACTOR_WEIGHTS}
    scores.update({"technical": 80.0, "momentum": 70.0})
    available = set(REGIME_FACTOR_WEIGHTS) - {"news"}
    value, info = weighted_regime_score(scores, available)
    assert info["dropped"] == ["news"]
    assert "news" not in info["weights"]
    assert abs(sum(info["weights"].values()) - 1.0) < 1e-9
    # news at its neutral 50 must not dilute the result
    expected = (80.0 * 0.25 + 70.0 * 0.20 + 50.0 * 0.20 + 50.0 * 0.15 + 50.0 * 0.10) / 0.90
    assert value == pytest.approx(expected, abs=1e-4)


def test_weighted_score_neutral_when_no_feed_available():
    value, info = weighted_regime_score({k: 50.0 for k in REGIME_FACTOR_WEIGHTS}, set())
    assert value == 50.0
    assert info["weights"] == {}


def test_missing_news_is_dropped_not_neutral_filled():
    """A run without news must not be dragged toward 50 by a phantom 0.10 weight."""
    without = build_market_regime_v2(
        market_rows(), macro_rows(), pd.DataFrame(), window_hours=24, run_time=RUN_TIME)
    assert without["drivers"]["dropped_factors"] == ["news"]
    assert "news" not in without["drivers"]["weights_used"]
    assert abs(sum(without["drivers"]["weights_used"].values()) - 1.0) < 1e-9

    with_news = build_market_regime_v2(
        market_rows(), macro_rows(), news_rows(opp=80, risk=10),
        window_hours=24, run_time=RUN_TIME)
    assert with_news["drivers"]["dropped_factors"] == []
    assert with_news["drivers"]["weights_used"]["news"] == pytest.approx(0.10)
    # news score of 70 pulls the composite away from the news-free value
    assert with_news["bullish_score"] != without["bullish_score"]


def test_factor_availability_flags_empty_feeds():
    available = factor_availability(market_rows(), macro_rows(), news_available=False)
    assert {"technical", "momentum", "breadth", "risk_appetite", "macro"} <= available
    assert "news" not in available
    empty = factor_availability(pd.DataFrame(), pd.DataFrame(), news_available=False)
    assert empty == set()


def test_correction_inside_bull_market_phase():
    assert classify_market_phase("uptrend", "fading", "healthy", "normal") == "late_bull"
    assert classify_market_phase("neutral", "negative", "healthy", "normal") == "correction_in_bull"


def test_low_confidence_mixed_regime():
    technical = market_rows(multiplier=0.0)
    technical["close"] = 100
    technical["ma_20"] = 100
    technical["ma_50"] = 100
    technical["ma_200"] = 100
    technical["macd_hist"] = 0
    signal = build_market_regime_v2(
        technical,
        macro_rows(vix_close=19, vix_chg=0.5, equity_chg=0.1),
        pd.DataFrame(),
        window_hours=24,
        run_time=RUN_TIME,
    )

    assert signal["market_regime"] in {"neutral", "risk_off"}
    assert signal["confidence"] < 0.35
