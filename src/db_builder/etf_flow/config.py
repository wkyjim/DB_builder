"""Configuration for deterministic ETF flow analytics."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ETFWindowConfig:
    short: int = 5
    medium: int = 20
    long: int = 60
    percentile: int = 252


@dataclass(frozen=True)
class ETFMinimumConfig:
    issuer_consensus_count: int = 2
    high_confidence_issuer_count: int = 3
    eligible_etf_count: int = 2
    history_days_for_zscore: int = 40


@dataclass(frozen=True)
class ETFOutlierConfig:
    max_daily_flow_pct_aum: float = 0.20
    winsor_lower_quantile: float = 0.01
    winsor_upper_quantile: float = 0.99


@dataclass(frozen=True)
class ETFScoreConfig:
    segment_score_weights: dict[str, float] = field(
        default_factory=lambda: {
            "zflow": 10.0,
            "momentum": 8.0,
            "persistence": 12.0,
            "breadth": 10.0,
            "consensus": 8.0,
            "concentration_penalty": 12.0,
        }
    )
    forward_setup_weights: dict[str, float] = field(
        default_factory=lambda: {
            "price_trend": 0.20,
            "relative_strength": 0.15,
            "market_breadth": 0.15,
            "etf_flow_persistence": 0.15,
            "flow_acceleration": 0.10,
            "cross_issuer_consensus": 0.10,
            "flow_breadth": 0.10,
            "regime_alignment": 0.05,
        }
    )


@dataclass(frozen=True)
class ETFRegimeConfig:
    weight_in_total_regime: float = 0.15


@dataclass(frozen=True)
class ETFExclusionConfig:
    leveraged: bool = True
    inverse: bool = True
    inactive: bool = True


@dataclass(frozen=True)
class ETFAnalyticsConfig:
    windows: ETFWindowConfig = field(default_factory=ETFWindowConfig)
    minimums: ETFMinimumConfig = field(default_factory=ETFMinimumConfig)
    outliers: ETFOutlierConfig = field(default_factory=ETFOutlierConfig)
    scores: ETFScoreConfig = field(default_factory=ETFScoreConfig)
    regime: ETFRegimeConfig = field(default_factory=ETFRegimeConfig)
    exclusions: ETFExclusionConfig = field(default_factory=ETFExclusionConfig)


def clamp(value: float | int | None, lower: float = 0.0, upper: float = 100.0) -> float:
    if value is None:
        return lower
    return max(lower, min(upper, float(value)))


def flow_signal_label(score: float | None) -> str:
    value = clamp(score)
    if value >= 80:
        return "strong inflow"
    if value >= 65:
        return "moderate inflow"
    if value >= 45:
        return "neutral"
    if value >= 30:
        return "moderate outflow"
    return "strong outflow"


def flow_regime_label(score: float | None) -> str:
    value = clamp(score)
    if value >= 80:
        return "strong risk-on"
    if value >= 65:
        return "moderate risk-on"
    if value >= 55:
        return "selective risk-on"
    if value >= 45:
        return "neutral / mixed"
    if value >= 35:
        return "defensive rotation"
    if value >= 20:
        return "moderate risk-off"
    return "strong risk-off"


def probability_bucket(score: float | None) -> str:
    value = clamp(score)
    if value >= 75:
        return "high positive setup"
    if value >= 60:
        return "moderate positive setup"
    if value >= 45:
        return "neutral"
    if value >= 30:
        return "moderate underperformance risk"
    return "high underperformance risk"
