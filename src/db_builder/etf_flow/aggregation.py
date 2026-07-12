"""Aggregate ETF flow features into segment, rotation, regime and report outputs."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pandas as pd

from db_builder.etf_flow.breadth import breadth_metrics
from db_builder.etf_flow.availability import analysis_now, availability_status, available_mask, issuer_availability_records
from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp, flow_signal_label
from db_builder.etf_flow.consensus import issuer_consensus
from db_builder.etf_flow.exposure_mapping import exposure_for_segment
from db_builder.etf_flow.feature_engineering import build_daily_flow_table, build_rolling_features
from db_builder.etf_flow.forward_signal import build_forward_signals
from db_builder.etf_flow.models import ETFAnalyticsOutput
from db_builder.etf_flow.regime import build_flow_regime
from db_builder.etf_flow.representative import (
    build_coverage_audit,
    build_divergence_flags as build_representative_divergence_flags,
    build_etf_flow_signal_daily,
    build_market_flow,
)


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
    consensus_score = clamp((consensus["issuer_weighted_consensus"] + 1.0) * 50.0)
    reliability = clamp(_safe_float(group["data_quality_score"].mean()))
    score = (
        50.0
        + ((zflow - 50.0) / 50.0) * weights["zflow"]
        + ((momentum - 50.0) / 50.0) * weights["momentum"]
        + ((persistence - 50.0) / 50.0) * weights["persistence"]
        + ((consensus_score - 50.0) / 50.0) * weights["consensus"]
        + ((reliability - 50.0) / 50.0) * weights["reliability"]
    )
    return clamp(score)


def _weight_series(group: pd.DataFrame, config: ETFAnalyticsConfig) -> pd.Series:
    base = pd.to_numeric(group.get("aum_lag1"), errors="coerce").fillna(pd.to_numeric(group.get("aum"), errors="coerce")).fillna(0)
    method = config.grouping.weighting_method
    if method == "equal":
        weights = pd.Series(1.0, index=group.index)
    elif method == "aum":
        weights = base
    elif method == "capped_aum":
        weights = base
    else:
        weights = base.clip(lower=0) ** 0.5
    total = weights.sum()
    if total <= 0:
        weights = pd.Series(1.0, index=group.index)
        total = weights.sum()
    weights = weights / total
    cap = config.grouping.max_single_etf_weight
    for _ in range(5):
        over = weights > cap
        if not over.any():
            break
        excess = (weights[over] - cap).sum()
        weights.loc[over] = cap
        under = ~over
        under_sum = weights[under].sum()
        if under_sum <= 0:
            break
        weights.loc[under] = weights[under] + (weights[under] / under_sum * excess)
    return weights / weights.sum()


def _weighted_average(group: pd.DataFrame, column: str, weights: pd.Series) -> float:
    values = pd.to_numeric(group.get(column), errors="coerce")
    valid = values.notna() & weights.notna()
    if not valid.any():
        return 0.0
    w = weights[valid]
    return float((values[valid] * w).sum() / w.sum())


def _issuer_agreement(group: pd.DataFrame, config: ETFAnalyticsConfig) -> tuple[float, str, int, int, int]:
    if group.empty:
        return 50.0, "neutral", 0, 0, 0
    issuer_rows = []
    for issuer, issuer_group in group.groupby("issuer", dropna=False):
        flow = pd.to_numeric(issuer_group.get("flow_1d"), errors="coerce").sum()
        aum = pd.to_numeric(issuer_group.get("aum_lag1"), errors="coerce").fillna(pd.to_numeric(issuer_group.get("aum"), errors="coerce")).sum()
        normalized = float(flow / aum) if aum else 0.0
        issuer_rows.append(normalized)
    band = config.availability.neutral_flow_pct_aum
    positive = sum(value > band for value in issuer_rows)
    negative = sum(value < -band for value in issuer_rows)
    neutral = len(issuer_rows) - positive - negative
    directional = max(positive, negative)
    if not issuer_rows:
        return 50.0, "neutral", 0, 0, 0
    agreement = clamp(((directional + neutral * 0.5) / len(issuer_rows)) * 100.0)
    direction = "positive" if positive > negative else "negative" if negative > positive else "neutral"
    return agreement, direction, positive, negative, neutral


def _reliability(
    *,
    coverage: float,
    reported_issuer_count: int,
    agreement_score: float,
    data_quality_score: float,
    classification_score: float,
    config: ETFAnalyticsConfig,
) -> float:
    coverage_score = clamp(coverage * 100.0)
    freshness_score = 100.0 if coverage >= config.availability.minimum_usable_coverage else 40.0
    issuer_score = agreement_score if reported_issuer_count >= 2 else 50.0
    reliability = (
        coverage_score * 0.35
        + freshness_score * 0.20
        + issuer_score * 0.20
        + data_quality_score * 0.15
        + classification_score * 0.10
    )
    if reported_issuer_count < 2:
        reliability = min(reliability, config.availability.single_issuer_reliability_cap)
    return clamp(reliability)


def build_exposure_aggregates(
    features: pd.DataFrame,
    config: ETFAnalyticsConfig | None = None,
    *,
    analysis_timestamp: datetime | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cfg = config or ETFAnalyticsConfig()
    if features.empty:
        return pd.DataFrame(), pd.DataFrame()
    timestamp = analysis_timestamp or analysis_now()
    df = features.copy()
    for column in ("exposure_id", "exposure_name", "exposure_type"):
        if column not in df:
            df[column] = None
    exposure_meta = df["primary_segment"].apply(exposure_for_segment)
    df["exposure_id"] = df["exposure_id"].fillna(exposure_meta.apply(lambda item: item.exposure_id))
    df["exposure_name"] = df["exposure_name"].fillna(exposure_meta.apply(lambda item: item.exposure_name))
    df["exposure_type"] = df["exposure_type"].fillna(exposure_meta.apply(lambda item: item.exposure_type))
    df["date"] = pd.to_datetime(df["date"]).dt.date
    dates = sorted(df["date"].dropna().unique())
    rows = []
    availability_rows = []
    for obs_date in dates:
        prior = df[df["date"] <= obs_date]
        availability_rows.append(
            issuer_availability_records(prior, analysis_timestamp=timestamp, effective_date=obs_date, config=cfg)
        )
        for exposure_id, all_history in prior.groupby("exposure_id", dropna=True):
            latest_by_ticker = all_history.sort_values(["ticker", "date"]).groupby("ticker", as_index=False).tail(1)
            current = latest_by_ticker[latest_by_ticker["date"].eq(obs_date)].copy()
            if current.empty:
                continue
            available = available_mask(current, timestamp)
            reported = current[available].copy()
            reported = reported[pd.to_numeric(reported.get("flow_1d"), errors="coerce").notna()]
            eligible_weight = _weight_series(latest_by_ticker, cfg)
            reported_weight = eligible_weight[eligible_weight.index.isin(reported.index)].sum()
            stale = latest_by_ticker["date"].apply(lambda value: (pd.Timestamp(obs_date) - pd.Timestamp(value)).days).max() > cfg.availability.stale_after_hours / 24.0
            status, provisional = availability_status(float(reported_weight), stale=bool(stale), config=cfg)
            if reported.empty:
                continue
            weights = _weight_series(reported, cfg)
            agreement_score, direction, positive, negative, neutral = _issuer_agreement(reported, cfg)
            reliability = _reliability(
                coverage=float(reported_weight),
                reported_issuer_count=int(reported["issuer"].nunique()),
                agreement_score=agreement_score,
                data_quality_score=_safe_float(reported["data_quality_score"].mean(), 50.0),
                classification_score=100.0 if exposure_id != "UNCLASSIFIED" else 40.0,
                config=cfg,
            )
            level = clamp(50.0 + _weighted_average(reported, "flow_20d_pct_aum", weights) * 2500.0)
            momentum = clamp(50.0 + _weighted_average(reported, "flow_momentum", weights) * 5000.0)
            persistence = clamp(_weighted_average(reported, "flow_persistence_20d", weights) * 100.0)
            acceleration = clamp(50.0 + _weighted_average(reported, "flow_acceleration", weights) * 5000.0)
            raw_score = clamp(level * 0.35 + momentum * 0.25 + persistence * 0.15 + acceleration * 0.10 + agreement_score * 0.15)
            adjusted_score = clamp(50.0 + (raw_score - 50.0) * reliability / 100.0)
            first = reported.iloc[0]
            rows.append(
                {
                    "date": obs_date,
                    "analysis_timestamp": timestamp,
                    "exposure_id": exposure_id,
                    "exposure_name": first.get("exposure_name"),
                    "exposure_type": first.get("exposure_type"),
                    "known_flow_1d": pd.to_numeric(reported.get("flow_1d"), errors="coerce").sum(),
                    "known_flow_5d": pd.to_numeric(reported.get("flow_5d"), errors="coerce").sum(),
                    "known_flow_20d": pd.to_numeric(reported.get("flow_20d"), errors="coerce").sum(),
                    "normalized_flow_1d": _weighted_average(reported, "flow_pct_aum_winsorized", weights),
                    "normalized_flow_5d": _weighted_average(reported, "flow_5d_pct_aum", weights),
                    "normalized_flow_20d": _weighted_average(reported, "flow_20d_pct_aum", weights),
                    "flow_momentum": _weighted_average(reported, "flow_momentum", weights),
                    "flow_acceleration": _weighted_average(reported, "flow_acceleration", weights),
                    "flow_persistence": _weighted_average(reported, "flow_persistence_20d", weights),
                    "reported_etf_count": int(reported["ticker"].nunique()),
                    "eligible_etf_count": int(latest_by_ticker["ticker"].nunique()),
                    "reported_issuer_count": int(reported["issuer"].nunique()),
                    "eligible_issuer_count": int(latest_by_ticker["issuer"].nunique()),
                    "issuer_aum_coverage": float(reported_weight),
                    "data_availability_status": status,
                    "issuer_agreement_score": agreement_score,
                    "signal_reliability": reliability,
                    "raw_flow_score": raw_score,
                    "adjusted_flow_score": adjusted_score,
                    "flow_signal": flow_signal_label(adjusted_score),
                    "is_provisional": provisional,
                    "direction_consensus": direction,
                    "positive_issuer_count": positive,
                    "negative_issuer_count": negative,
                    "neutral_issuer_count": neutral,
                }
            )
    availability = pd.concat([row for row in availability_rows if not row.empty], ignore_index=True) if availability_rows else pd.DataFrame()
    return pd.DataFrame(rows), availability


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
        if _safe_float(row.get("confidence")) < 40:
            flags.append(
                {
                    "date": as_of,
                    "flag_type": "low_flow_reliability",
                    "severity": "low",
                    "segment": segment,
                    "description": "ETF flow evidence has low reliability and should be treated as provisional.",
                    "suggested_fix": "Use grouped exposure reliability before treating flow as confirmation.",
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
    features["flow_momentum"] = features["flow_ema_5"] - features["flow_ema_20"]
    representative_signals = build_etf_flow_signal_daily(daily, raw, cfg)
    representative_market_flow = build_market_flow(representative_signals)
    representative_divergences = build_representative_divergence_flags(representative_signals, cfg)
    coverage_audit = build_coverage_audit(raw)
    segments = build_segment_aggregates(features, cfg)
    exposures, issuer_availability = build_exposure_aggregates(features, cfg)
    consensus = build_consensus_table(segments)
    rotation = build_rotation_table(segments)
    forward = build_forward_signals(segments, cfg)
    latest_segments = pd.DataFrame()
    as_of = None
    if not segments.empty:
        as_of = max(segments["date"])
        latest_segments = segments[segments["date"] == as_of].copy()
    latest_exposures = pd.DataFrame()
    if not exposures.empty:
        as_of = max(exposures["date"])
        latest_exposures = exposures[exposures["date"] == as_of].copy()
    regime = build_flow_regime(latest_exposures if not latest_exposures.empty else latest_segments, existing_regime_score=existing_regime_score, config=cfg)
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
        exposures=latest_exposures.sort_values("adjusted_flow_score", ascending=False).head(20).to_dict(orient="records") if not latest_exposures.empty else [],
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
            "representative_available_count": int(coverage_audit["available"].sum()) if not coverage_audit.empty else 0,
            "representative_total_count": int(len(coverage_audit)) if not coverage_audit.empty else 0,
            "avg_data_quality_score": float(features["data_quality_score"].mean()) if not features.empty else 0.0,
            "rows_loaded": int(len(features)),
        },
        representative_signals=representative_signals[representative_signals["date"].eq(representative_signals["date"].max())].sort_values("state_strength", ascending=False).head(60).to_dict(orient="records") if not representative_signals.empty else [],
        market_flow=representative_market_flow,
        representative_divergences=representative_divergences.to_dict(orient="records") if not representative_divergences.empty else [],
        coverage_audit=coverage_audit.to_dict(orient="records") if not coverage_audit.empty else [],
    )
    tables = {
        "daily": daily,
        "features": features,
        "segments": segments,
        "exposures": exposures,
        "issuer_availability": issuer_availability,
        "consensus": consensus,
        "rotation": rotation,
        "forward": forward,
        "audits": audits,
        "representative_signals": representative_signals,
        "representative_market_flow": pd.DataFrame([representative_market_flow]) if representative_market_flow else pd.DataFrame(),
        "representative_divergences": representative_divergences,
        "coverage_audit": coverage_audit,
    }
    return output, tables


def json_safe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = df.to_dict(orient="records")
    return json.loads(json.dumps(records, default=str))
