"""Flow-based confidence adjustment."""

from __future__ import annotations

from db_builder.etf_flow.config import clamp


def flow_confidence_adjustment(
    *,
    base_confidence: float,
    agreement_ratio: float,
    contradiction_count: int,
    missing_feature_count: int,
    flow_data_quality: float,
    flow_coverage: float,
    concentration_penalty: float,
) -> dict:
    if flow_coverage < 0.5 or flow_data_quality < 50:
        bonus = 0.0
    else:
        bonus = max(0.0, agreement_ratio - 0.5) * 20.0
    penalty = contradiction_count * 7.5 + missing_feature_count * 3.0 + concentration_penalty * 15.0
    final = clamp(base_confidence + bonus - penalty)
    return {
        "base_confidence": base_confidence,
        "flow_agreement_bonus": bonus,
        "flow_contradiction_penalty": contradiction_count * 7.5,
        "low_coverage_penalty": 0.0 if flow_coverage >= 0.5 else 15.0,
        "concentration_penalty": concentration_penalty * 15.0,
        "final_signal_confidence": final,
    }
