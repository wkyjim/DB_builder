"""ETF flow regime scoring."""

from __future__ import annotations

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp, flow_regime_label


RISK_ON_SEGMENTS = {
    "Broad Equity",
    "Total U.S. Equity",
    "Small Caps",
    "Mid Caps",
    "Growth",
    "High Yield Credit",
    "Semiconductors",
    "Technology",
}

DEFENSIVE_SEGMENTS = {
    "Treasury Bills",
    "U.S. Treasuries",
    "Long Duration Treasury",
    "Short Treasury",
    "Gold",
    "Core Bonds",
    "Investment Grade Credit",
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
    risk_on = seg[seg["segment"].isin(RISK_ON_SEGMENTS)]
    defensive = seg[seg["segment"].isin(DEFENSIVE_SEGMENTS)]
    risk_on_score = float(risk_on["score"].mean()) if not risk_on.empty else 50.0
    defensive_score = float(defensive["score"].mean()) if not defensive.empty else 50.0
    breadth = float(seg["flow_breadth_20d"].mean() * 100.0) if "flow_breadth_20d" in seg else 50.0
    flow_score = clamp((risk_on_score * 0.55) + ((100.0 - defensive_score) * 0.25) + (breadth * 0.20))
    confidence = clamp(float(seg["confidence"].mean()) if "confidence" in seg else 50.0)
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
            "flow_breadth_score": breadth,
        },
    }
