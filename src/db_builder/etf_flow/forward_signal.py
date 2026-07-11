"""Heuristic forward setup features for later validation."""

from __future__ import annotations

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp, probability_bucket


def build_forward_signals(segments: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    cfg = config or ETFAnalyticsConfig()
    if segments.empty:
        return pd.DataFrame()
    rows = []
    weights = cfg.scores.forward_setup_weights
    for _, row in segments.iterrows():
        flow = clamp((float(row.get("score") or 50) - 50) * 2 + 50)
        price = clamp(float(row.get("avg_price_trend_score") or 50))
        breadth = clamp(float(row.get("flow_breadth_20d") or 0) * 100)
        consensus = clamp((float(row.get("issuer_consensus") or 0) + 1) * 50)
        regime_alignment = clamp((flow + consensus) / 2)
        score = (
            price * weights["price_trend"]
            + price * weights["relative_strength"]
            + breadth * weights["market_breadth"]
            + flow * weights["etf_flow_persistence"]
            + clamp((float(row.get("flow_momentum") or 0) * 5000) + 50) * weights["flow_acceleration"]
            + consensus * weights["cross_issuer_consensus"]
            + breadth * weights["flow_breadth"]
            + regime_alignment * weights["regime_alignment"]
        )
        score = clamp(score)
        catalysts = []
        invalidators = []
        if float(row.get("flow_breadth_20d") or 0) >= 0.6:
            catalysts.append("broad ETF flow participation")
        if abs(float(row.get("issuer_consensus") or 0)) >= 0.5:
            catalysts.append("cross-issuer flow agreement")
        if float(row.get("concentration_penalty") or 0) >= 0.4:
            invalidators.append("flow concentrated in a small number of funds or issuers")
        if float(row.get("confidence") or 0) < 50:
            invalidators.append("low flow evidence confidence")
        rows.append(
            {
                "date": row["date"],
                "segment_type": row.get("segment_type", "primary_segment"),
                "segment": row["segment"],
                "outperformance_score": score,
                "probability_bucket": probability_bucket(score),
                "flow_contribution": flow,
                "price_contribution": price,
                "breadth_contribution": breadth,
                "regime_contribution": regime_alignment,
                "confidence": row.get("confidence"),
                "catalysts": catalysts,
                "invalidators": invalidators,
            }
        )
    return pd.DataFrame(rows)
