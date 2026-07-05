"""Deterministic contradiction checks for market update reports."""

from __future__ import annotations


def audit_report_scores(scores: dict) -> list[dict]:
    flags = []
    regime = scores.get("regime", {})
    strength = scores.get("market_strength", {})
    breadth = strength.get("breadth", {})
    sectors = scores.get("sectors", [])
    themes = scores.get("themes", [])
    news = scores.get("news", {})
    confidence = scores.get("confidence", {})

    if regime.get("score", 50) >= 65 and breadth.get("score", 50) < 45:
        flags.append(
            {
                "severity": "high",
                "section": "Market Regime Score",
                "issue": "Regime score is risk-on while breadth is weak.",
                "deterministic_fix": "Label as rotation/narrow leadership until breadth improves.",
            }
        )
    if confidence.get("score", 100) < 40 and regime.get("score", 50) >= 80:
        flags.append(
            {
                "severity": "medium",
                "section": "Evidence Quality / Confidence",
                "issue": "Strong regime score has low evidence quality.",
                "deterministic_fix": "Keep high regime score but mark confidence as low.",
            }
        )
    for sector in sectors:
        if sector.get("score", 50) >= 70 and sector.get("trend_label") in {"downtrend", "strong downtrend"}:
            flags.append(
                {
                    "severity": "medium",
                    "section": "Sector Strength Ranking",
                    "issue": f"{sector.get('sector')} score is high but trend label is bearish.",
                    "deterministic_fix": "Downgrade trend-confirmation language.",
                }
            )
        if sector.get("score", 50) >= 65 and sector.get("breadth_label") == "weak":
            flags.append(
                {
                    "severity": "medium",
                    "section": "Sector Strength Ranking",
                    "issue": f"{sector.get('sector')} score is high but breadth participation is weak.",
                    "deterministic_fix": "Mark as narrow leadership.",
                }
            )
    for theme in themes:
        if theme.get("news_confirmation") and not theme.get("price_confirmation"):
            flags.append(
                {
                    "severity": "low",
                    "section": "Theme Strength Ranking",
                    "issue": f"{theme.get('theme')} has strong news confirmation but weak price confirmation.",
                    "deterministic_fix": "Classify as unconfirmed narrative.",
                }
            )
    if news.get("score", 50) >= 65 and regime.get("score", 50) < 45:
        flags.append(
            {
                "severity": "medium",
                "section": "News Analytics",
                "issue": "News sentiment is bullish while price/cross-asset regime is risk-off.",
                "deterministic_fix": "Treat headlines as unconfirmed by price.",
            }
        )
    return flags
