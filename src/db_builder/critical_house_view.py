"""Deterministic critical PM house view for investment reports."""

from __future__ import annotations

from dataclasses import dataclass


SECTOR_ORDER = [
    "Cybersecurity",
    "Healthcare",
    "Semiconductors",
    "Technology",
    "Financials",
    "Real Estate",
    "Defense",
    "Energy",
    "Utilities",
    "Consumer Discretionary",
    "Grid Infrastructure",
    "Nuclear",
    "Crypto",
]


@dataclass(frozen=True)
class SectorView:
    sector: str
    report_bias: str
    critical_view: str
    opportunities: str
    key_risks: str
    portfolio_bias: str


def _float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def _first(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


def _by_key(rows: list[dict], key: str) -> dict[str, dict]:
    return {str(row.get(key)): row for row in rows if row.get(key)}


def _theme_contains(row: dict, *needles: str) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ["theme_name", "parent_theme", "sector_name", "dimension_value"]
    ).lower()
    return any(needle.lower() in text for needle in needles)


def _best_theme(themes: list[dict], *needles: str) -> dict:
    matches = [row for row in themes if _theme_contains(row, *needles)]
    if not matches:
        return {}
    return max(matches, key=lambda row: _float(row.get("secular_score")))


def confidence_interpretation(confidence: float) -> str:
    if confidence < 0.15:
        return "Low confidence: avoid strong directional language and treat the view as mixed, fragile, and selective."
    if confidence <= 0.35:
        return "Moderate-low confidence: directional conclusions should remain tentative and risk-controlled."
    if confidence > 0.50:
        return "Higher confidence: stronger directional language is acceptable if market internals confirm it."
    return "Moderate confidence: respect the signal, but confirm with breadth and volatility."


def market_interpretation(data: dict) -> tuple[str, list[str], str]:
    old_regime = _first(data.get("regime") or {}).get("regime_label", "unknown")
    regime_v2 = _first(data.get("regime_v2") or {})
    v2_regime = str(regime_v2.get("market_regime") or "unknown")
    phase = str(regime_v2.get("market_phase") or "unknown")
    confidence = _float(regime_v2.get("confidence"))
    volatility = str(regime_v2.get("volatility_state") or "unknown")
    breadth = str(regime_v2.get("breadth_state") or "unknown")
    momentum = str(regime_v2.get("momentum_state") or "unknown")
    opportunities = data.get("opportunities") or []
    macro = data.get("macro") or []
    watchlist = data.get("watchlist") or []

    flags: list[str] = []
    if old_regime == "risk_on" and v2_regime in {"neutral", "range_bound"} and confidence < 0.20:
        flags.append(
            "Old news-led regime says risk_on, but Market Regime 2.0 is neutral/range_bound with low confidence."
        )

    vix_spike = any(str(row.get("symbol")) == "^VIX" and _float(row.get("pct_chg")) > 20 for row in macro)
    tech_drawdown = any(
        str(row.get("ticker")) in {"QQQ", "SMH", "SOXX"}
        and (_float(row.get("pct_chg")) <= -4 or _float(row.get("return_20d")) <= -5)
        for row in watchlist
    )
    if vix_spike and tech_drawdown and not opportunities:
        flags.append(
            "Volatility and lack of high-conviction opportunities argue against aggressive broad beta exposure."
        )

    if breadth == "healthy" and momentum == "stable_positive":
        flags.append("Underlying bull structure is not broken, but tactical risk is elevated.")

    if phase == "correction_in_bull":
        final_view = "Correction inside bull market"
    elif old_regime == "risk_on" and v2_regime in {"neutral", "range_bound"} and confidence < 0.20:
        final_view = "Neutral-to-fragile risk-on"
    elif confidence < 0.15:
        final_view = "Selective risk, not broad risk-on"
    elif volatility in {"stressed", "elevated"} and not opportunities:
        final_view = "Selective risk, not broad risk-on"
    else:
        final_view = v2_regime.replace("_", " ").title() if v2_regime != "unknown" else old_regime.replace("_", " ").title()

    sentiment = "Mixed overall unless broad risk-on confirmation appears."
    if volatility in {"stressed", "elevated"}:
        sentiment = "Mixed and tactically fragile: stressed volatility offsets constructive secular leadership."
    return final_view, flags, sentiment


def _rotation_rank(rotation_by_sector: dict[str, dict], sector: str) -> int:
    return int(_float(rotation_by_sector.get(sector, {}).get("rotation_rank"), default=999))


def _sector_regime(regime_by_sector: dict[str, dict], sector: str) -> str:
    return str(regime_by_sector.get(sector, {}).get("sector_regime") or "unknown")


def _strong_secular_theme(themes: list[dict], *needles: str) -> dict:
    row = _best_theme(themes, *needles)
    if _float(row.get("secular_score")) > 70:
        return row
    return {}


def build_sector_views(data: dict) -> list[SectorView]:
    regimes = _by_key(data.get("sector_regimes") or [], "sector_name")
    rotation = _by_key(data.get("sector_rotation") or [], "sector_name")
    themes = data.get("secular_themes") or []
    views: list[SectorView] = []

    for sector in SECTOR_ORDER:
        regime = _sector_regime(regimes, sector)
        rank = _rotation_rank(rotation, sector)
        bias = str(rotation.get(sector, {}).get("allocation_bias") or "neutral").replace("_", " ").title()
        critical = "Mixed; wait for clearer confirmation."
        opportunities = "Selective exposure only."
        risks = "Low confidence or missing tactical confirmation."
        portfolio = "Neutral"

        if sector == "Cybersecurity" and rank <= 3 and _strong_secular_theme(themes, "cybersecurity"):
            critical = "Best risk-adjusted overweight"
            opportunities = "Leadership in sector rotation and durable security spending."
            risks = "Crowding and broad technology volatility."
            portfolio = "Overweight"
        elif sector == "Healthcare" and regime in {"bull", "strong_bull"}:
            critical = "Defensive growth overweight"
            opportunities = "Constructive regime with GLP-1 and healthcare leadership."
            risks = "Policy and FDA headline risk."
            portfolio = "Overweight"
        elif sector == "Semiconductors":
            ai = _strong_secular_theme(themes, "AI Infrastructure", "Advanced AI Compute")
            if ai:
                critical = "Selective overweight, do not chase rebounds"
                opportunities = "AI infrastructure remains a powerful secular driver."
                risks = "Crowded positioning, valuation risk, and capex disappointment."
                portfolio = "Selective Overweight"
        elif sector == "Technology":
            critical = "Neutral to mild overweight"
            opportunities = "AI leadership remains supportive."
            risks = "Stressed volatility argues against chasing broad beta."
            portfolio = "Neutral / Mild Overweight"
        elif sector == "Defense":
            critical = "Structural overweight, tactical neutral"
            opportunities = "Defense spending and geopolitics support the long-term thesis."
            risks = "Tactical rank is not always top-tier."
            portfolio = "Neutral / Accumulate on weakness"
        elif sector == "Energy":
            energy = _strong_secular_theme(themes, "Energy Security")
            if regime == "strong_bear" and energy:
                critical = "Tactical underweight, structural optionality"
                opportunities = "Energy security remains a secular option."
                risks = "Current trend and rotation are weak."
                portfolio = "Underweight tactically"
        elif sector == "Nuclear":
            nuclear = _strong_secular_theme(themes, "Nuclear")
            if regime == "strong_bear" and nuclear:
                critical = "Long-term bullish, short-term correction"
                opportunities = "Do not sell the structural thesis; accumulate slowly."
                risks = "Near-term momentum is poor."
                portfolio = "Tactical Underweight / Accumulate slowly"
        elif sector == "Grid Infrastructure":
            grid = _strong_secular_theme(themes, "Grid Expansion", "Power Demand")
            if grid:
                critical = "Accumulate on weakness"
                opportunities = "Power demand and grid expansion remain secularly strong."
                risks = "Tactical weakness can persist."
                portfolio = "Neutral / Accumulate"
        elif sector == "Consumer Discretionary" and regime == "strong_bear":
            critical = "Underweight"
            opportunities = "Only tactical rebounds until trend improves."
            risks = "Weak regime and risk-off consumer sensitivity."
            portfolio = "Underweight"
        elif sector == "Crypto":
            critical = "Trading allocation only"
            opportunities = "High beta upside when risk appetite improves."
            risks = "Not defensive; drawdowns can be abrupt."
            portfolio = "Tactical only"
        elif sector in {"Financials", "Real Estate", "Utilities"}:
            critical = f"{bias} with tactical confirmation required"
            opportunities = "Use ETF-level confirmation before adding."
            risks = "Rates, credit, and volatility sensitivity."
            portfolio = bias

        views.append(SectorView(sector, bias, critical, opportunities, risks, portfolio))
    return views


def opportunity_view(opportunities: list[dict]) -> str:
    if opportunities:
        return "High-conviction filtered opportunities are present; size by risk score and technical confirmation."
    return (
        "No high-conviction single-name opportunities passed strict filters. This is a cautious/neutral signal, "
        "not an absence of investable themes. Tier 1: Cybersecurity, Healthcare, Selective Semiconductors. "
        "Tier 2: Defense, Grid Infrastructure, AI Infrastructure on pullbacks. "
        "Tier 3: Nuclear, Energy Security, Crypto tactical only."
    )


def final_house_view(view: str, data: dict) -> str:
    opportunities = data.get("opportunities") or []
    regime_v2 = _first(data.get("regime_v2") or {})
    volatility = str(regime_v2.get("volatility_state") or "unknown")
    if not opportunities and volatility in {"stressed", "elevated"}:
        return (
            "The market is neutral/range-bound with selective risk appetite, elevated volatility, and narrow leadership. "
            "The best opportunities remain cybersecurity, healthcare, and selective AI infrastructure, but broad beta "
            "should not be chased while volatility is stressed and the opportunity scanner remains empty."
        )
    return (
        f"The house view is {view.lower()}. Favor sectors with confirmed rotation and secular support, "
        "while keeping position sizing tied to volatility, breadth, and scanner confirmation."
    )


def build_critical_house_view(data: dict) -> dict:
    regime_v2 = _first(data.get("regime_v2") or {})
    confidence = _float(regime_v2.get("confidence"))
    view, flags, sentiment = market_interpretation(data)
    sectors = build_sector_views(data)
    opp_view = opportunity_view(data.get("opportunities") or [])
    final_view = final_house_view(view, data)
    positioning = "Selective risk"
    if confidence < 0.15:
        positioning = "Selective risk / tactical caution"
    if "Volatility and lack" in " ".join(flags):
        positioning = "Selective risk, not broad beta"
    return {
        "market_view": view,
        "market_interpretation": (
            f"{view}: old and new regime signals should be reconciled through confidence, volatility, breadth, "
            "and opportunity availability rather than read as clean risk-on."
        ),
        "confidence_interpretation": confidence_interpretation(confidence),
        "sentiment_view": sentiment,
        "contradiction_flags": flags,
        "positioning_bias": positioning,
        "sector_views": [view.__dict__ for view in sectors],
        "opportunity_view": opp_view,
        "what_would_change_view": [
            "Sustained VIX cooling with QQQ/SMH stabilization would support more risk.",
            "A broader opportunity scanner hit-rate would argue for higher exposure.",
            "Breakdown in breadth or 50/200-day ETF trends would move the view defensive.",
        ],
        "final_house_view": final_view,
    }


def render_critical_pm_view(view: dict) -> str:
    lines = [
        "## Critical PM View",
        "",
        f"- Market interpretation: **{view['market_interpretation']}**",
        f"- Confidence interpretation: {view['confidence_interpretation']}",
        f"- Sentiment view: {view['sentiment_view']}",
        f"- Positioning bias: **{view['positioning_bias']}**",
    ]
    if view.get("contradiction_flags"):
        lines.extend(["", "### Internal Contradiction Flags", ""])
        lines.extend([f"- {flag}" for flag in view["contradiction_flags"]])
    lines.extend(["", "### Opportunity And Risk Decision", "", f"- {view['opportunity_view']}"])
    lines.extend(["", "### What Would Change The View", ""])
    lines.extend([f"- {item}" for item in view["what_would_change_view"]])
    return "\n".join(lines).rstrip() + "\n"


def render_sector_critical_table(view: dict) -> str:
    lines = [
        "## Sector-by-Sector Critical View",
        "",
        "| Sector | Report Bias | Critical View | Opportunities | Key Risks | Portfolio Bias |",
        "|---|---|---|---|---|---|",
    ]
    for row in view.get("sector_views", []):
        lines.append(
            f"| {row['sector']} | {row['report_bias']} | {row['critical_view']} | "
            f"{row['opportunities']} | {row['key_risks']} | {row['portfolio_bias']} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_final_house_view(view: dict) -> str:
    return f"## Final House View\n\n{view['final_house_view']}\n"
