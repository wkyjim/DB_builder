"""Aggregate ETF flow features into segment, rotation, regime and report outputs."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd

from db_builder.etf_flow.breadth import breadth_metrics
from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp, flow_signal_label
from db_builder.etf_flow.consensus import issuer_consensus
from db_builder.etf_flow.feature_engineering import build_daily_flow_table, build_rolling_features
from db_builder.etf_flow.forward_signal import build_forward_signals
from db_builder.etf_flow.models import ETFAnalyticsOutput
from db_builder.etf_flow.regime import build_flow_regime


def _safe_float(value, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if pd.isna(numeric):
        return default
    return numeric


def _top_contributors(rows: pd.DataFrame, column: str, *, ascending: bool = False) -> list[dict[str, Any]]:
    if rows.empty or column not in rows:
        return []
    selected = rows.sort_values(column, ascending=ascending).head(3)
    return [
        {
            "ticker": item.get("ticker"),
            "issuer": item.get("issuer"),
            "value": _safe_float(item.get(column)),
        }
        for _, item in selected.iterrows()
    ]


def _segment_score(group: pd.DataFrame, metrics: dict, consensus: dict, config: ETFAnalyticsConfig) -> float:
    weights = config.scores.segment_score_weights
    zflow = clamp((_safe_float(group["flow_zscore_20"].mean()) * 10.0) + 50.0)
    momentum = clamp((_safe_float(group["flow_ema_5"].mean()) - _safe_float(group["flow_ema_20"].mean())) * 5000.0 + 50.0)
    persistence = clamp(_safe_float(group["flow_persistence_20d"].mean()) * 100.0)
    breadth = clamp(metrics["flow_breadth_20d"] * 100.0)
    consensus_score = clamp((consensus["issuer_weighted_consensus"] + 1.0) * 50.0)
    penalty = clamp(metrics["top_1_flow_share"] * 100.0)
    score = (
        50.0
        + ((zflow - 50.0) / 50.0) * weights["zflow"]
        + ((momentum - 50.0) / 50.0) * weights["momentum"]
        + ((persistence - 50.0) / 50.0) * weights["persistence"]
        + ((breadth - 50.0) / 50.0) * weights["breadth"]
        + ((consensus_score - 50.0) / 50.0) * weights["consensus"]
        - (penalty / 100.0) * weights["concentration_penalty"]
    )
    return clamp(score)


def build_segment_aggregates(features: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    cfg = config or ETFAnalyticsConfig()
    if features.empty:
        return pd.DataFrame()
    rows = []
    latest_dates = sorted(features["date"].dropna().unique())
    for obs_date in latest_dates:
        daily = features[features["date"] == obs_date]
        for segment, group in daily.groupby("primary_segment", dropna=True):
            if not segment or len(group) < 1:
                continue
            metrics = breadth_metrics(group)
            consensus = issuer_consensus(group)
            score = _segment_score(group, metrics, consensus, cfg)
            confidence = clamp(
                (_safe_float(group["data_quality_score"].mean()) * 0.45)
                + (consensus["confidence"] * 0.30)
                + (min(len(group), 5) / 5.0 * 25.0)
                - (metrics["top_1_flow_share"] * 15.0)
            )
            rows.append(
                {
                    "date": obs_date,
                    "segment_type": "primary_segment",
                    "segment": segment,
                    "flow_1d": group["flow_1d"].sum(),
                    "flow_5d": group["flow_5d"].sum(),
                    "flow_20d": group["flow_20d"].sum(),
                    "flow_60d": group["flow_60d"].sum(),
                    "flow_pct_aum_5d": group["flow_5d_pct_aum"].mean(),
                    "flow_pct_aum_20d": group["flow_20d_pct_aum"].mean(),
                    "flow_zscore_20d": group["flow_zscore_20"].mean(),
                    "flow_momentum": group["flow_ema_5"].mean() - group["flow_ema_20"].mean(),
                    "flow_breadth_20d": metrics["flow_breadth_20d"],
                    "issuer_consensus": consensus["issuer_weighted_consensus"],
                    "concentration_penalty": metrics["top_1_flow_share"],
                    "score": score,
                    "signal": flow_signal_label(score),
                    "confidence": confidence,
                    "top_contributors": _top_contributors(group, "flow_20d", ascending=False),
                    "top_detractors": _top_contributors(group, "flow_20d", ascending=True),
                    "eligible_fund_count": len(group),
                    "issuer_count": int(group["issuer"].nunique()),
                    "data_quality_score": group["data_quality_score"].mean(),
                    "avg_price_trend_score": group["price_trend_score"].mean(),
                    **metrics,
                    **{f"consensus_{key}": value for key, value in consensus.items()},
                }
            )
    return pd.DataFrame(rows)


def build_consensus_table(segments: pd.DataFrame) -> pd.DataFrame:
    if segments.empty:
        return pd.DataFrame()
    rows = []
    for _, row in segments.iterrows():
        rows.append(
            {
                "date": row["date"],
                "segment": row["segment"],
                "positive_issuer_count": row.get("consensus_positive_issuer_count"),
                "negative_issuer_count": row.get("consensus_negative_issuer_count"),
                "neutral_issuer_count": row.get("consensus_neutral_issuer_count"),
                "positive_etf_count": row.get("consensus_positive_etf_count"),
                "negative_etf_count": row.get("consensus_negative_etf_count"),
                "issuer_weighted_consensus": row.get("consensus_issuer_weighted_consensus"),
                "equal_weight_consensus": row.get("consensus_equal_weight_consensus"),
                "aum_weighted_consensus": row.get("consensus_aum_weighted_consensus"),
                "flow_direction_agreement_ratio": row.get("consensus_flow_direction_agreement_ratio"),
                "cross_issuer_dispersion": row.get("consensus_cross_issuer_dispersion"),
                "dominant_issuer_share": row.get("consensus_dominant_issuer_share"),
                "issuer_concentration_penalty": row.get("consensus_issuer_concentration_penalty"),
                "direction": row.get("consensus_direction"),
                "confidence": row.get("consensus_confidence"),
            }
        )
    return pd.DataFrame(rows)


def build_rotation_table(segments: pd.DataFrame) -> pd.DataFrame:
    if segments.empty:
        return pd.DataFrame()
    df = segments.sort_values(["segment_type", "segment", "date"]).copy()
    df["rank_today"] = df.groupby(["date", "segment_type"])["score"].rank(ascending=False, method="first").astype(int)
    df["rank_5d_ago"] = df.groupby(["segment_type", "segment"])["rank_today"].shift(5)
    df["rank_20d_ago"] = df.groupby(["segment_type", "segment"])["rank_today"].shift(20)
    df["score_5d_ago"] = df.groupby(["segment_type", "segment"])["score"].shift(5)
    df["score_20d_ago"] = df.groupby(["segment_type", "segment"])["score"].shift(20)
    df["rank_change_5d"] = df["rank_5d_ago"] - df["rank_today"]
    df["rank_change_20d"] = df["rank_20d_ago"] - df["rank_today"]
    df["flow_score_change_5d"] = df["score"] - df["score_5d_ago"]
    df["flow_score_change_20d"] = df["score"] - df["score_20d_ago"]
    df["flow_acceleration"] = df.groupby(["segment_type", "segment"])["flow_momentum"].diff(5)
    df["price_relative_strength_change"] = df.groupby(["segment_type", "segment"])["avg_price_trend_score"].diff(5)

    def status(row) -> str:
        rank = _safe_float(row.get("rank_today"), 999)
        change = _safe_float(row.get("rank_change_5d"), 0)
        score = _safe_float(row.get("score"), 50)
        if rank <= 3 and change >= 3:
            return "new leader"
        if rank <= 5 and score >= 65:
            return "strengthening leader"
        if score >= 65:
            return "stable leader"
        if change >= 3:
            return "early improvement"
        if score <= 35 and change <= -3:
            return "new laggard"
        if score <= 35:
            return "persistent laggard"
        if change <= -3:
            return "deteriorating"
        return "mixed"

    df["rotation_status"] = [status(row) for _, row in df.iterrows()]
    return df[
        [
            "date",
            "segment_type",
            "segment",
            "rank_today",
            "rank_5d_ago",
            "rank_20d_ago",
            "rank_change_5d",
            "rank_change_20d",
            "score",
            "flow_score_change_5d",
            "flow_score_change_20d",
            "flow_acceleration",
            "price_relative_strength_change",
            "rotation_status",
        ]
    ].rename(columns={"score": "flow_score"})


def build_audit_flags(segments: pd.DataFrame, regime: dict) -> pd.DataFrame:
    flags = []
    if segments.empty:
        return pd.DataFrame(flags)
    as_of = max(segments["date"])
    latest = segments[segments["date"] == as_of]
    for _, row in latest.iterrows():
        segment = row["segment"]
        if _safe_float(row.get("confidence")) > _safe_float(row.get("data_quality_score")) + 15:
            flags.append(
                {
                    "date": as_of,
                    "flag_type": "confidence_above_data_quality",
                    "severity": "medium",
                    "segment": segment,
                    "description": "Flow confidence is materially higher than data quality.",
                    "suggested_fix": "Cap confidence or improve ETF source coverage.",
                }
            )
        if int(row.get("issuer_count") or 0) <= 1 and _safe_float(row.get("confidence")) >= 70:
            flags.append(
                {
                    "date": as_of,
                    "flag_type": "single_issuer_high_confidence",
                    "severity": "high",
                    "segment": segment,
                    "description": "High confidence is based on one issuer only.",
                    "suggested_fix": "Require multi-issuer confirmation before high confidence.",
                }
            )
        if _safe_float(row.get("concentration_penalty")) >= 0.75:
            flags.append(
                {
                    "date": as_of,
                    "flag_type": "flow_concentration",
                    "severity": "medium",
                    "segment": segment,
                    "description": "A small number of ETFs dominates the segment flow signal.",
                    "suggested_fix": "Treat the signal as concentrated rather than broad confirmation.",
                }
            )
    if regime.get("conflict_flag"):
        flags.append(
            {
                "date": as_of,
                "flag_type": "regime_flow_conflict",
                "severity": "medium",
                "segment": "market",
                "description": "ETF flow regime materially differs from the existing market regime score.",
                "suggested_fix": "Show combined regime and conflict flag instead of overwriting market regime.",
            }
        )
    return pd.DataFrame(flags)


def build_etf_flow_analytics(
    raw: pd.DataFrame,
    *,
    existing_regime_score: float | None = None,
    config: ETFAnalyticsConfig | None = None,
) -> tuple[ETFAnalyticsOutput, dict[str, pd.DataFrame]]:
    cfg = config or ETFAnalyticsConfig()
    daily = build_daily_flow_table(raw, cfg)
    features = build_rolling_features(daily, cfg)
    segments = build_segment_aggregates(features, cfg)
    consensus = build_consensus_table(segments)
    rotation = build_rotation_table(segments)
    forward = build_forward_signals(segments, cfg)
    latest_segments = pd.DataFrame()
    as_of = None
    if not segments.empty:
        as_of = max(segments["date"])
        latest_segments = segments[segments["date"] == as_of].copy()
    regime = build_flow_regime(latest_segments, existing_regime_score=existing_regime_score, config=cfg)
    audits = build_audit_flags(segments, regime)
    price_flow = []
    if not features.empty:
        latest_features = features[features["date"] == max(features["date"])]
        price_flow = (
            latest_features.sort_values(["data_quality_score", "flow_20d"], ascending=[False, False])
            .head(12)[["date", "ticker", "primary_segment", "price_flow_state", "price_trend_score", "flow_20d_pct_aum", "data_quality_score"]]
            .rename(columns={"primary_segment": "segment", "flow_20d_pct_aum": "flow_trend"})
            .to_dict(orient="records")
        )
    output = ETFAnalyticsOutput(
        as_of_date=as_of,
        flow_regime=regime,
        market_segments=latest_segments.sort_values("score", ascending=False).head(15).to_dict(orient="records") if not latest_segments.empty else [],
        price_flow_signals=price_flow,
        rotation={
            "new_leaders": rotation[rotation["rotation_status"].eq("new leader")].tail(5)["segment"].tolist() if not rotation.empty else [],
            "improving": rotation[rotation["rotation_status"].isin(["early improvement", "strengthening leader"])].tail(5)["segment"].tolist() if not rotation.empty else [],
            "deteriorating": rotation[rotation["rotation_status"].eq("deteriorating")].tail(5)["segment"].tolist() if not rotation.empty else [],
            "persistent_laggards": rotation[rotation["rotation_status"].eq("persistent laggard")].tail(5)["segment"].tolist() if not rotation.empty else [],
        },
        contradictions=audits.to_dict(orient="records") if not audits.empty else [],
        forward_signals=forward[forward["date"].eq(as_of)].sort_values("outperformance_score", ascending=False).head(12).to_dict(orient="records") if not forward.empty and as_of else [],
        data_quality={
            "eligible_etf_count": int(features["ticker"].nunique()) if not features.empty else 0,
            "avg_data_quality_score": float(features["data_quality_score"].mean()) if not features.empty else 0.0,
            "rows_loaded": int(len(features)),
        },
    )
    tables = {
        "daily": daily,
        "features": features,
        "segments": segments,
        "consensus": consensus,
        "rotation": rotation,
        "forward": forward,
        "audits": audits,
    }
    return output, tables


def json_safe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = df.to_dict(orient="records")
    return json.loads(json.dumps(records, default=str))
