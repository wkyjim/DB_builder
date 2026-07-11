"""Data-quality checks for ETF flow observations."""

from __future__ import annotations

import math

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or pd.isna(value)


def row_missing_flags(row: pd.Series, config: ETFAnalyticsConfig | None = None) -> list[str]:
    cfg = config or ETFAnalyticsConfig()
    flags: list[str] = []
    if _is_missing(row.get("shares_outstanding")):
        flags.append("missing_shares_outstanding")
    if _is_missing(row.get("nav")):
        flags.append("missing_nav")
    if _is_missing(row.get("aum")):
        flags.append("missing_aum")
    if _is_missing(row.get("primary_segment")):
        flags.append("missing_classification")
    for field in ("shares_outstanding", "nav", "aum"):
        value = row.get(field)
        if not _is_missing(value) and float(value) < 0:
            flags.append(f"negative_{field}")
    flow_pct = row.get("flow_pct_aum_raw")
    if not _is_missing(flow_pct) and abs(float(flow_pct)) > cfg.outliers.max_daily_flow_pct_aum:
        flags.append("extreme_flow_pct_aum")
    return flags


def data_quality_score(flags: list[str]) -> float:
    score = 100.0
    penalties = {
        "missing_shares_outstanding": 30.0,
        "missing_nav": 25.0,
        "missing_aum": 15.0,
        "missing_classification": 10.0,
        "negative_shares_outstanding": 50.0,
        "negative_nav": 50.0,
        "negative_aum": 50.0,
        "extreme_flow_pct_aum": 20.0,
    }
    for flag in flags:
        score -= penalties.get(flag, 5.0)
    return clamp(score)


def attach_quality_scores(df: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    result = df.copy()
    flags = [row_missing_flags(row, config) for _, row in result.iterrows()]
    result["missing_data_flags"] = flags
    result["data_quality_score"] = [data_quality_score(items) for items in flags]
    result["flow_valid"] = result["data_quality_score"] >= 60
    return result
