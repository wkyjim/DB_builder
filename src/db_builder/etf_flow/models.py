"""Typed structures used by ETF flow analytics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class ETFAnalyticsOutput:
    as_of_date: date | None
    flow_regime: dict[str, Any] = field(default_factory=dict)
    market_segments: list[dict[str, Any]] = field(default_factory=list)
    exposures: list[dict[str, Any]] = field(default_factory=list)
    price_flow_signals: list[dict[str, Any]] = field(default_factory=list)
    rotation: dict[str, Any] = field(default_factory=dict)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    forward_signals: list[dict[str, Any]] = field(default_factory=list)
    data_quality: dict[str, Any] = field(default_factory=dict)
    representative_signals: list[dict[str, Any]] = field(default_factory=list)
    market_flow: dict[str, Any] = field(default_factory=dict)
    representative_divergences: list[dict[str, Any]] = field(default_factory=list)
    coverage_audit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "flow_regime": self.flow_regime,
            "market_segments": self.market_segments,
            "exposures": self.exposures,
            "price_flow_signals": self.price_flow_signals,
            "rotation": self.rotation,
            "contradictions": self.contradictions,
            "forward_signals": self.forward_signals,
            "data_quality": self.data_quality,
            "representative_signals": self.representative_signals,
            "market_flow": self.market_flow,
            "representative_divergences": self.representative_divergences,
            "coverage_audit": self.coverage_audit,
        }
