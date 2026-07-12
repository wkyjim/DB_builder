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
        consensus = clamp((float(row.get("issuer_consensus") or 0) + 1) * 50)
        regime_alignment = clamp((flow + consensus) / 2)
        score = (
            price * weights["price_trend"]
            + price * weights["relative_strength"]
            + price * weights["market_breadth"]
            + flow * weights["etf_flow_persistence"]
            + clamp((float(row.get("flow_momentum") or 0) * 5000) + 50) * weights["flow_acceleration"]
            + consensus * weights["cross_issuer_consensus"]
            + regime_alignment * weights["regime_alignment"]
        )
        score = clamp(score)
        catalysts = []
        invalidators = []
        if abs(float(row.get("issuer_consensus") or 0)) >= 0.5:
            catalysts.append("cross-issuer flow agreement")
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
                "breadth_contribution": None,
                "regime_contribution": regime_alignment,
                "confidence": row.get("confidence"),
                "catalysts": catalysts,
                "invalidators": invalidators,
            }
        )
    return pd.DataFrame(rows)
