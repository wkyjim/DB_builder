"""Flow normalization utilities."""

from __future__ import annotations

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig


def winsorize_series(series: pd.Series, config: ETFAnalyticsConfig | None = None) -> pd.Series:
    cfg = config or ETFAnalyticsConfig()
    numeric = pd.to_numeric(series, errors="coerce")
    lower = numeric.quantile(cfg.outliers.winsor_lower_quantile)
    upper = numeric.quantile(cfg.outliers.winsor_upper_quantile)
    hard = cfg.outliers.max_daily_flow_pct_aum
    lower = max(lower, -hard) if pd.notna(lower) else -hard
    upper = min(upper, hard) if pd.notna(upper) else hard
    return numeric.clip(lower=lower, upper=upper)


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    mean = numeric.rolling(window, min_periods=max(5, window // 2)).mean()
    std = numeric.rolling(window, min_periods=max(5, window // 2)).std()
    return (numeric - mean) / std.replace(0, pd.NA)


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.rolling(window, min_periods=max(5, window // 4)).rank(pct=True)
