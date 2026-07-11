"""ETF flow momentum, acceleration, and persistence."""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").ewm(span=span, adjust=False, min_periods=1).mean()


def rolling_slope(series: pd.Series, window: int) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric - numeric.shift(window - 1)


def classify_acceleration(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "unknown"
    if value >= 0.01:
        return "strongly accelerating"
    if value >= 0.0025:
        return "accelerating"
    if value <= -0.01:
        return "strongly decelerating"
    if value <= -0.0025:
        return "decelerating"
    return "stable"
