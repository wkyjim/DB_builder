from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.market_regime_v2 import build_market_regime_v2, classify_market_phase


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
