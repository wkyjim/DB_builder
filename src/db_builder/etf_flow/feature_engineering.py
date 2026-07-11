"""Base daily ETF flow calculations and rolling features."""

from __future__ import annotations

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig
from db_builder.etf_flow.momentum import ema, rolling_slope
from db_builder.etf_flow.normalization import rolling_percentile, rolling_zscore, winsorize_series
from db_builder.etf_flow.price_flow import price_flow_state, price_trend_score, trend_direction
from db_builder.etf_flow.validation import attach_quality_scores


def build_daily_flow_table(raw: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    cfg = config or ETFAnalyticsConfig()
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df = df.sort_values(["ticker", "date"])
    for column in ("shares_outstanding", "nav", "close", "aum", "volume"):
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    grouped = df.groupby("ticker", group_keys=False)
    df["shares_lag1"] = grouped["shares_outstanding"].shift(1)
    df["nav_lag1"] = grouped["nav"].shift(1)
    df["aum_lag1"] = grouped["aum"].shift(1)
    df["aum_avg_20d"] = grouped["aum"].transform(lambda x: x.rolling(cfg.windows.medium, min_periods=1).mean())
    df["shares_change"] = df["shares_outstanding"] - df["shares_lag1"]
    df["estimated_flow"] = df["shares_change"] * df["nav"]
    df["estimated_flow_lag_nav"] = df["shares_change"] * df["nav_lag1"]
    df["flow_pct_aum"] = df["estimated_flow"] / df["aum"].replace(0, pd.NA)
    df["flow_pct_aum_lag"] = df["estimated_flow"] / df["aum_lag1"].replace(0, pd.NA)
    df["flow_pct_aum_raw"] = df["flow_pct_aum_lag"].fillna(df["flow_pct_aum"])
    df["flow_pct_aum_winsorized"] = grouped["flow_pct_aum_raw"].transform(lambda x: winsorize_series(x, cfg))
    price = df["close"].fillna(df["nav"])
    df["return_1d"] = grouped.apply(lambda x: price.loc[x.index].pct_change(), include_groups=False).reset_index(level=0, drop=True)
    return attach_quality_scores(df, cfg)


def build_rolling_features(daily: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    cfg = config or ETFAnalyticsConfig()
    if daily.empty:
        return pd.DataFrame()
    df = daily.copy().sort_values(["ticker", "date"])
    grouped = df.groupby("ticker", group_keys=False)
    flow_pct = "flow_pct_aum_winsorized"
    df["flow_1d"] = df["estimated_flow"]
    for window in (cfg.windows.short, cfg.windows.medium, cfg.windows.long):
        df[f"flow_{window}d"] = grouped["estimated_flow"].transform(lambda x, w=window: x.rolling(w, min_periods=1).sum())
        df[f"flow_{window}d_pct_aum"] = grouped[flow_pct].transform(lambda x, w=window: x.rolling(w, min_periods=1).sum())
    df["flow_ema_5"] = grouped[flow_pct].transform(lambda x: ema(x, cfg.windows.short))
    df["flow_ema_20"] = grouped[flow_pct].transform(lambda x: ema(x, cfg.windows.medium))
    df["flow_slope_5"] = grouped[flow_pct].transform(lambda x: rolling_slope(x, cfg.windows.short))
    df["flow_slope_20"] = grouped[flow_pct].transform(lambda x: rolling_slope(x, cfg.windows.medium))
    df["flow_acceleration"] = (df["flow_ema_5"] - df["flow_ema_20"]) - grouped.apply(
        lambda x: (x["flow_ema_5"] - x["flow_ema_20"]).shift(cfg.windows.short),
        include_groups=False,
    ).reset_index(level=0, drop=True)
    df["flow_zscore_20"] = grouped[flow_pct].transform(lambda x: rolling_zscore(x, cfg.windows.medium))
    df["flow_zscore_60"] = grouped[flow_pct].transform(lambda x: rolling_zscore(x, cfg.windows.long))
    df["flow_percentile_252"] = grouped[flow_pct].transform(lambda x: rolling_percentile(x, cfg.windows.percentile))
    df["positive_flow_days_5d"] = grouped["estimated_flow"].transform(lambda x: (x > 0).rolling(cfg.windows.short, min_periods=1).sum())
    df["positive_flow_days_20d"] = grouped["estimated_flow"].transform(lambda x: (x > 0).rolling(cfg.windows.medium, min_periods=1).sum())
    df["flow_persistence_20d"] = df["positive_flow_days_20d"] / cfg.windows.medium
    df["return_5d"] = grouped["return_1d"].transform(lambda x: (1 + x.fillna(0)).rolling(cfg.windows.short, min_periods=1).apply(lambda y: y.prod() - 1))
    df["return_20d"] = grouped["return_1d"].transform(lambda x: (1 + x.fillna(0)).rolling(cfg.windows.medium, min_periods=1).apply(lambda y: y.prod() - 1))
    df["price_trend_score"] = [
        price_trend_score(row.get("return_5d"), row.get("return_20d"))
        for _, row in df.iterrows()
    ]
    df["price_flow_state"] = [
        price_flow_state(
            trend_direction(row.get("return_20d")),
            trend_direction(row.get("flow_20d_pct_aum"), positive=0.0005, negative=-0.0005),
        )
        for _, row in df.iterrows()
    ]
    df = df.rename(columns={"flow_5d_pct_aum": "flow_5d_pct_aum", "flow_20d_pct_aum": "flow_20d_pct_aum", "flow_60d_pct_aum": "flow_60d_pct_aum"})
    return df
