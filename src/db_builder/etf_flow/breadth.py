"""ETF flow breadth and concentration metrics."""

from __future__ import annotations

import pandas as pd


def breadth_metrics(rows: pd.DataFrame) -> dict:
    if rows.empty:
        return {
            "flow_breadth_1d": 0.0,
            "flow_breadth_5d": 0.0,
            "flow_breadth_20d": 0.0,
            "aum_weighted_breadth_20d": 0.0,
            "median_normalized_flow": 0.0,
            "flow_dispersion": 0.0,
            "top_1_flow_share": 0.0,
            "top_3_flow_share": 0.0,
            "hhi": 1.0,
            "effective_fund_count": 1.0,
        }
    df = rows.copy()
    count = max(len(df), 1)
    total_aum = df["aum"].fillna(0).sum()
    inflow_aum = df.loc[df["flow_20d"] > 0, "aum"].fillna(0).sum()
    abs_flow = df["flow_20d"].abs().fillna(0)
    total_abs = abs_flow.sum()
    shares = abs_flow / total_abs if total_abs > 0 else pd.Series([1.0 / count] * count, index=df.index)
    return {
        "flow_breadth_1d": float((df["flow_1d"] > 0).sum() / count),
        "flow_breadth_5d": float((df["flow_5d"] > 0).sum() / count),
        "flow_breadth_20d": float((df["flow_20d"] > 0).sum() / count),
        "aum_weighted_breadth_20d": float(inflow_aum / total_aum) if total_aum > 0 else 0.0,
        "median_normalized_flow": float(df["flow_20d_pct_aum"].median()) if df["flow_20d_pct_aum"].notna().any() else 0.0,
        "flow_dispersion": float(df["flow_20d_pct_aum"].std()) if len(df) > 1 else 0.0,
        "top_1_flow_share": float(shares.sort_values(ascending=False).head(1).sum()),
        "top_3_flow_share": float(shares.sort_values(ascending=False).head(3).sum()),
        "hhi": float((shares**2).sum()),
        "effective_fund_count": float(1.0 / (shares**2).sum()) if (shares**2).sum() > 0 else 1.0,
    }
