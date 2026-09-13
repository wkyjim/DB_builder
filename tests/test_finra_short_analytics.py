from datetime import date, timedelta

import numpy as np
import pandas as pd

from db_builder.finra_short_analytics import compute_daily_short_volume_features
from db_builder.short_regime import (
    classify_short_regime,
    compute_price_relative_features,
    compute_short_pressure_effectiveness,
    merge_short_interest_point_in_time,
    short_flow_confirmation,
)


def daily_rows(count=330, spike_last=False):
    rows = []
    start = date(2025, 1, 2)
    for index in range(count):
        ratio = 0.4 + (index % 5) * 0.002
        if spike_last and index == count - 1:
            ratio = 0.9
        rows.append(
            {
                "trade_date": start + timedelta(days=index),
                "ticker": "AAA",
                "short_volume": ratio * 1000,
                "short_exempt_volume": 10,
                "total_volume": 1000,
                "data_quality_status": "valid",
            }
        )
    return rows


def test_daily_svr_windows_and_lagged_zscore():
    frame = compute_daily_short_volume_features(daily_rows(spike_last=True))
    last = frame.iloc[-1]

    assert last["svr_5d"] > last["svr_20d"]
    assert last["svr_z20"] > 2
    assert last["svr_pctile_20d_1y"] > 0.95


def test_daily_casv_uses_strictly_lagged_baseline():
    frame = compute_daily_short_volume_features(daily_rows(count=100, spike_last=True))
    last = frame.iloc[-1]

    assert last["abnormal_svr_60d"] > 0.45
    assert last["casv_5d"] > 0.45


def test_zero_total_volume_is_excluded():
    rows = daily_rows(count=50)
    rows[-1]["total_volume"] = 0
    frame = compute_daily_short_volume_features(rows)

    assert len(frame) == 49


def test_point_in_time_merge_never_uses_future_publication():
    daily = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA"],
            "analytics_date": [date(2026, 1, 20), date(2026, 1, 28)],
            "short_activity_score": [50, 50],
        }
    )
    si = pd.DataFrame(
        {
            "ticker": ["AAA"],
            "settlement_date": [date(2026, 1, 15)],
            "publication_date": [date(2026, 1, 27)],
            "short_interest": [1000],
        }
    )
    merged = merge_short_interest_point_in_time(daily, si)

    assert pd.isna(merged.iloc[0]["short_interest"])
    assert merged.iloc[1]["short_interest"] == 1000


def test_short_pressure_effectiveness_does_not_leak_future_return():
    dates = pd.date_range("2026-01-01", periods=80, freq="B")
    base = pd.DataFrame(
        {
            "ticker": "AAA",
            "analytics_date": dates,
            "daily_return": 0.0,
            "benchmark_return": 0.0,
            "svr_pctile_20d_1y": 0.9,
        }
    )
    first = compute_short_pressure_effectiveness(base.copy())
    changed = base.copy()
    changed.loc[changed.index[-1], "daily_return"] = -0.50
    second = compute_short_pressure_effectiveness(changed)

    assert first.iloc[-6]["short_pressure_effectiveness"] == second.iloc[-6]["short_pressure_effectiveness"]


def test_price_relative_features_use_spy_instead_of_self_benchmark():
    dates = pd.date_range("2025-01-02", periods=280, freq="B")
    rows = []
    prices = {"AAA": 100.0, "SPY": 100.0}
    for index, analytics_date in enumerate(dates):
        prices["AAA"] *= 1.004 if index % 5 else 0.998
        prices["SPY"] *= 1.001 if index % 5 else 0.999
        for ticker in ("AAA", "SPY"):
            rows.append(
                {
                    "date": analytics_date,
                    "ticker": ticker,
                    "close": prices[ticker],
                    "volume": 1_000_000,
                    "ma_20": prices[ticker] * 0.99,
                    "ma_50": prices[ticker] * 0.98,
                    "ma_100": prices[ticker] * 0.97,
                    "ma_200": prices[ticker] * 0.96,
                    "rsi_14": 55,
                    "macd": 1,
                    "macd_signal": 0.8,
                    "macd_hist": 0.2,
                }
            )

    featured = compute_price_relative_features(pd.DataFrame(rows))
    latest = featured.loc[featured["ticker"].eq("AAA")].iloc[-1]

    assert latest["rel_return_1m"] > 0
    assert latest["rel_return_3m"] > 0
    assert latest["rel_return_12m"] > 0
    assert latest["up_capture"] != 1
    assert latest["down_capture"] != 1


def test_price_relative_features_are_independent_of_batch_peers():
    dates = pd.date_range("2025-01-02", periods=140, freq="B")
    rows = []
    for ticker, daily_return in (("AAA", 0.002), ("BBB", -0.001), ("SPY", 0.0005)):
        close = 100.0
        for analytics_date in dates:
            close *= 1 + daily_return
            rows.append(
                {
                    "date": analytics_date,
                    "ticker": ticker,
                    "close": close,
                    "volume": 1_000_000,
                    "ma_20": close,
                    "ma_50": close,
                    "ma_100": close,
                    "ma_200": close,
                    "rsi_14": 50,
                    "macd": 0,
                    "macd_signal": 0,
                    "macd_hist": 0,
                }
            )
    prices = pd.DataFrame(rows)

    narrow = compute_price_relative_features(prices[prices["ticker"].isin(["AAA", "SPY"])]).query("ticker == 'AAA'")
    wide = compute_price_relative_features(prices).query("ticker == 'AAA'")

    assert narrow.iloc[-1]["rel_return_3m"] == wide.iloc[-1]["rel_return_3m"]
    assert narrow.iloc[-1]["up_capture"] == wide.iloc[-1]["up_capture"]


def test_flow_confirmation_distinguishes_turnover_from_build():
    assert short_flow_confirmation(0.0, 90, -0.01)[0] == "HIGH_SHORT_TURNOVER_NO_BUILD"
    assert short_flow_confirmation(0.12, 90, -0.03)[0] == "CONFIRMED_SHORT_ACCUMULATION"


def test_regime_guardrails_distinguish_crowding_and_unwind():
    row = pd.Series(
        {
            "short_pressure_effectiveness": 75,
            "si_change_1obs": 0.0,
            "days_to_cover": 8,
            "rel_return_1m": -0.05,
            "si_persistence_12m": 0.9,
        }
    )
    scores = {
        "short_position_score": 90,
        "short_activity_score": 85,
        "funding_short_score": 75,
        "unwind_risk_score": 35,
        "flow_confirmation_signal": "STABLE_STRUCTURAL_SHORT",
        "components": {},
    }
    regime, _, _ = classify_short_regime(row, scores)
    assert regime == "CROWDED_SHORT"

    row["si_change_1obs"] = -0.12
    scores["unwind_risk_score"] = 85
    regime, _, _ = classify_short_regime(row, scores)
    assert regime == "CONFIRMED_FUNDING_SHORT_UNWIND"
