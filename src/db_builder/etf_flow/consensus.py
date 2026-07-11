"""Cross-issuer consensus calculations."""

from __future__ import annotations

import pandas as pd


def consensus_direction(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def issuer_consensus(rows: pd.DataFrame) -> dict:
    if rows.empty:
        return {
            "positive_issuer_count": 0,
            "negative_issuer_count": 0,
            "neutral_issuer_count": 0,
            "positive_etf_count": 0,
            "negative_etf_count": 0,
            "issuer_weighted_consensus": 0.0,
            "equal_weight_consensus": 0.0,
            "aum_weighted_consensus": 0.0,
            "flow_direction_agreement_ratio": 0.0,
            "cross_issuer_dispersion": 0.0,
            "dominant_issuer_share": 0.0,
            "issuer_concentration_penalty": 1.0,
            "direction": "neutral",
            "confidence": 0.0,
        }
    df = rows.copy()
    df["issuer"] = df["issuer"].fillna("Unknown")
    issuer_flow = df.groupby("issuer")["flow_20d_pct_aum"].mean()
    issuer_direction = issuer_flow.map(consensus_direction)
    positive_issuer_count = int((issuer_direction == "positive").sum())
    negative_issuer_count = int((issuer_direction == "negative").sum())
    neutral_issuer_count = int((issuer_direction == "neutral").sum())
    issuer_count = max(len(issuer_direction), 1)
    dominant = max(positive_issuer_count, negative_issuer_count, neutral_issuer_count)
    agreement = dominant / issuer_count
    total_abs_flow = df["flow_20d"].abs().sum()
    issuer_abs = df.groupby("issuer")["flow_20d"].apply(lambda x: x.abs().sum())
    dominant_share = float(issuer_abs.max() / total_abs_flow) if total_abs_flow not in (0, None) and pd.notna(total_abs_flow) else 0.0
    concentration_penalty = min(1.0, max(0.0, dominant_share - 0.5) * 2)
    equal_weight = float(df["flow_20d_pct_aum"].mean()) if df["flow_20d_pct_aum"].notna().any() else 0.0
    aum_weighted = 0.0
    if df["aum"].fillna(0).sum() > 0:
        aum_weighted = float((df["flow_20d_pct_aum"].fillna(0) * df["aum"].fillna(0)).sum() / df["aum"].fillna(0).sum())
    direction = "positive" if positive_issuer_count > negative_issuer_count else "negative" if negative_issuer_count > positive_issuer_count else "neutral"
    signed = 1 if direction == "positive" else -1 if direction == "negative" else 0
    confidence = max(0.0, min(100.0, agreement * 100.0 * (1.0 - concentration_penalty)))
    return {
        "positive_issuer_count": positive_issuer_count,
        "negative_issuer_count": negative_issuer_count,
        "neutral_issuer_count": neutral_issuer_count,
        "positive_etf_count": int((df["flow_20d"] > 0).sum()),
        "negative_etf_count": int((df["flow_20d"] < 0).sum()),
        "issuer_weighted_consensus": signed * agreement * (1.0 - concentration_penalty),
        "equal_weight_consensus": equal_weight,
        "aum_weighted_consensus": aum_weighted,
        "flow_direction_agreement_ratio": agreement,
        "cross_issuer_dispersion": float(df["flow_20d_pct_aum"].std()) if len(df) > 1 else 0.0,
        "dominant_issuer_share": dominant_share,
        "issuer_concentration_penalty": concentration_penalty,
        "direction": direction,
        "confidence": confidence,
    }
