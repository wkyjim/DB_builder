"""Research scaffold for ETF flow feature validation.

This module intentionally does not claim predictive power. It prepares the
interfaces needed to test rank IC, hit rate, quintile spreads, turnover and
signal decay without using future data in feature construction.
"""

from __future__ import annotations

import pandas as pd


def attach_forward_relative_returns(
    features: pd.DataFrame,
    *,
    benchmark_ticker: str = "SPY",
    horizons: tuple[int, ...] = (5, 20, 60),
) -> pd.DataFrame:
    if features.empty:
        return features.copy()
    df = features.sort_values(["ticker", "date"]).copy()
    prices = df[["date", "ticker", "close"]].copy() if "close" in df.columns else pd.DataFrame()
    if prices.empty:
        return df
    for horizon in horizons:
        df[f"forward_return_{horizon}d"] = df.groupby("ticker")["close"].transform(lambda x: x.shift(-horizon) / x - 1)
        benchmark = df[df["ticker"].eq(benchmark_ticker)][["date", f"forward_return_{horizon}d"]].rename(
            columns={f"forward_return_{horizon}d": f"benchmark_forward_return_{horizon}d"}
        )
        df = df.merge(benchmark, on="date", how="left")
        df[f"forward_relative_return_{horizon}d"] = df[f"forward_return_{horizon}d"] - df[f"benchmark_forward_return_{horizon}d"]
    return df


def rank_information_coefficient(df: pd.DataFrame, *, score_column: str, return_column: str) -> float | None:
    usable = df[[score_column, return_column]].dropna()
    if len(usable) < 5:
        return None
    return float(usable[score_column].corr(usable[return_column], method="spearman"))


def quintile_spread(df: pd.DataFrame, *, score_column: str, return_column: str) -> float | None:
    usable = df[[score_column, return_column]].dropna().copy()
    if len(usable) < 10:
        return None
    usable["quintile"] = pd.qcut(usable[score_column], 5, labels=False, duplicates="drop")
    top = usable[usable["quintile"].eq(usable["quintile"].max())][return_column].mean()
    bottom = usable[usable["quintile"].eq(usable["quintile"].min())][return_column].mean()
    return float(top - bottom)
