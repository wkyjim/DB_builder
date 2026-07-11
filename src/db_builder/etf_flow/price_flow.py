"""Price x flow matrix classification."""

from __future__ import annotations

import pandas as pd


def trend_direction(value: float | None, *, positive: float = 0.001, negative: float = -0.001) -> str:
    if value is None or pd.isna(value):
        return "flat"
    numeric = float(value)
    if numeric > positive:
        return "positive"
    if numeric < negative:
        return "negative"
    return "flat"


def price_flow_state(price_trend: str, flow_trend: str) -> str:
    matrix = {
        ("positive", "positive"): "Accumulation",
        ("positive", "negative"): "Price Up / Flow Out",
        ("negative", "positive"): "Buying Weakness",
        ("negative", "negative"): "Distribution",
        ("flat", "positive"): "Quiet Accumulation",
        ("flat", "negative"): "Quiet Redemption",
        ("positive", "flat"): "Price-Led Move",
        ("negative", "flat"): "Price Weakness Without Confirmed Outflow",
    }
    return matrix.get((price_trend, flow_trend), "Mixed / Unconfirmed")


def price_trend_score(return_5d: float | None, return_20d: float | None, close_vs_nav: float | None = None) -> float:
    score = 50.0
    for value, weight in ((return_5d, 20.0), (return_20d, 30.0), (close_vs_nav, 5.0)):
        if value is None or pd.isna(value):
            continue
        score += max(-1.0, min(1.0, float(value) / 0.05)) * weight
    return max(0.0, min(100.0, score))
