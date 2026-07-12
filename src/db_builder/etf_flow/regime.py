"""ETF flow regime scoring."""

from __future__ import annotations

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp, flow_regime_label


RISK_ON_SEGMENTS = {
    "US_BROAD_EQUITY",
    "US_SMALL_CAP",
    "US_MID_CAP",
    "US_LARGE_GROWTH",
    "US_SEMICONDUCTORS",
    "US_TECHNOLOGY",
    "US_HIGH_YIELD",
    "EMERGING_MARKETS",
}

DEFENSIVE_SEGMENTS = {
    "US_TREASURY_BILLS",
    "US_TREASURY_LONG",
    "US_TREASURY_INTERMEDIATE",
    "US_TREASURY_SHORT",
    "GOLD",
    "US_CORE_BONDS",
    "US_INVESTMENT_GRADE",
    "US_UTILITIES",
    "US_CONSUMER_STAPLES",
}


def build_flow_regime(
    segments: pd.DataFrame,
    *,
    existing_regime_score: float | None = None,
    config: ETFAnalyticsConfig | None = None,
) -> dict:
    cfg = config or ETFAnalyticsConfig()
    if segments.empty:
        return {
            "score": 50.0,
            "label": "neutral / mixed",
            "confidence": 0.0,
            "existing_regime_score": existing_regime_score,
            "combined_regime_score": existing_regime_score,
            "conflict_flag": False,
        }
    seg = segments.copy()
    id_col = "exposure_id" if "exposure_id" in seg else "segment"
    score_col = "adjusted_flow_score" if "adjusted_flow_score" in seg else "score"
    confidence_col = "signal_reliability" if "signal_reliability" in seg else "confidence"
    risk_on = seg[seg[id_col].isin(RISK_ON_SEGMENTS)]
    defensive = seg[seg[id_col].isin(DEFENSIVE_SEGMENTS)]
    risk_on_score = float(risk_on[score_col].mean()) if not risk_on.empty else 50.0
    defensive_score = float(defensive[score_col].mean()) if not defensive.empty else 50.0
    reliability = float(seg[confidence_col].mean()) if confidence_col in seg else 50.0
    raw_flow_score = clamp((risk_on_score * 0.65) + ((100.0 - defensive_score) * 0.35))
    flow_score = clamp(50.0 + (raw_flow_score - 50.0) * reliability / 100.0)
    confidence = clamp(reliability)
    combined = None
    if existing_regime_score is not None:
        combined = clamp(
            (1.0 - cfg.regime.weight_in_total_regime) * existing_regime_score
            + cfg.regime.weight_in_total_regime * flow_score
        )
    conflict = bool(existing_regime_score is not None and abs(float(existing_regime_score) - flow_score) >= 20.0)
    dominant = "risk demand" if risk_on_score > defensive_score + 5 else "defensive demand" if defensive_score > risk_on_score + 5 else "mixed allocation"
    return {
        "score": flow_score,
        "label": flow_regime_label(flow_score),
        "confidence": confidence,
        "existing_regime_score": existing_regime_score,
        "combined_regime_score": combined,
        "conflict_flag": conflict,
        "dominant_allocation_direction": dominant,
        "components": {
            "risk_on_score": risk_on_score,
            "defensive_score": defensive_score,
            "reliability_adjusted_flow_score": flow_score,
            "average_signal_reliability": reliability,
        },
    }
