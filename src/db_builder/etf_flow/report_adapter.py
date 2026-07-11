"""Report adapter for ETF flow analytics output."""

from __future__ import annotations

from typing import Any
import math


def _fmt_money(value) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(numeric) or math.isinf(numeric):
        return "n/a"
    sign = "-" if numeric < 0 else ""
    return f"{sign}${abs(numeric):,.0f}"


def _fmt(value, digits: int = 1) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(numeric) or math.isinf(numeric):
        return "n/a"
    return f"{numeric:.{digits}f}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["No rows available."]
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
        *["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows],
    ]


def etf_flow_report_lines(data: dict[str, Any]) -> list[str]:
    if not data:
        return ["## ETF Flow Analytics", "", "No ETF flow analytics are available yet.", ""]
    regime = data.get("flow_regime") or {}
    segments = data.get("market_segments") or []
    forward = data.get("forward_signals") or []
    contradictions = data.get("contradictions") or []
    lines = ["## ETF Flow Analytics", ""]
    if regime:
        lines.extend(
            [
                "### ETF Flow Executive Summary",
                "",
                f"- ETF flow regime: **{regime.get('flow_regime_label') or regime.get('label', 'n/a')}**",
                f"- Flow regime score: `{_fmt(regime.get('flow_regime_score') or regime.get('score'))}`",
                f"- Flow confidence: `{_fmt(regime.get('flow_regime_confidence') or regime.get('confidence'))}`",
                f"- Dominant allocation direction: `{regime.get('dominant_allocation_direction', 'n/a')}`",
                f"- Regime conflict flag: `{regime.get('regime_conflict_flag') if 'regime_conflict_flag' in regime else regime.get('conflict_flag', False)}`",
                "",
            ]
        )
    lines.extend(["### Market Flow Dashboard", ""])
    lines.extend(
        _table(
            ["Segment", "1D Flow", "5D Flow", "20D Flow", "Flow % AUM 20D", "Score", "Signal", "Confidence"],
            [
                [
                    row.get("segment", "n/a"),
                    _fmt_money(row.get("flow_1d")),
                    _fmt_money(row.get("flow_5d")),
                    _fmt_money(row.get("flow_20d")),
                    _fmt(row.get("flow_pct_aum_20d"), 4),
                    _fmt(row.get("score")),
                    row.get("signal", "n/a"),
                    _fmt(row.get("confidence")),
                ]
                for row in segments[:12]
            ],
        )
    )
    lines.append("")
    lines.extend(["### Flow-Confirmed Forward Setups", ""])
    lines.extend(
        _table(
            ["Segment", "Setup Score", "Bucket", "Flow", "Breadth", "Confidence"],
            [
                [
                    row.get("segment", "n/a"),
                    _fmt(row.get("outperformance_score")),
                    row.get("probability_bucket", "n/a"),
                    _fmt(row.get("flow_contribution")),
                    _fmt(row.get("breadth_contribution")),
                    _fmt(row.get("confidence")),
                ]
                for row in forward[:10]
            ],
        )
    )
    lines.append("")
    lines.extend(["### ETF Flow Contradiction Flags", ""])
    lines.extend(
        _table(
            ["Severity", "Type", "Segment", "Description"],
            [
                [
                    row.get("severity", "n/a"),
                    row.get("flag_type", "n/a"),
                    row.get("segment", "n/a"),
                    row.get("description", "n/a"),
                ]
                for row in contradictions[:10]
            ],
        )
    )
    lines.append("")
    return lines
