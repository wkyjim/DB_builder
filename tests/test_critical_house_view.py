from __future__ import annotations

from db_builder.critical_house_view import build_critical_house_view


def base_data():
    return {
        "regime": [{"regime_label": "risk_on", "confidence_score": 0.17}],
        "regime_v2": [
            {
                "market_regime": "neutral",
                "market_phase": "range_bound",
                "confidence": 0.0949,
                "market_strength": "weak",
                "trend_state": "neutral",
                "momentum_state": "stable_positive",
                "volatility_state": "stressed",
                "breadth_state": "healthy",
                "risk_appetite_state": "neutral",
            }
        ],
        "sector_regimes": [
            {"sector_name": "Cybersecurity", "sector_regime": "bull"},
            {"sector_name": "Healthcare", "sector_regime": "bull"},
            {"sector_name": "Semiconductors", "sector_regime": "bull"},
            {"sector_name": "Technology", "sector_regime": "neutral"},
            {"sector_name": "Consumer Discretionary", "sector_regime": "strong_bear"},
            {"sector_name": "Nuclear", "sector_regime": "strong_bear"},
        ],
        "sector_rotation": [
            {"sector_name": "Cybersecurity", "rotation_rank": 1, "allocation_bias": "overweight"},
            {"sector_name": "Semiconductors", "rotation_rank": 2, "allocation_bias": "overweight"},
        ],
        "secular_themes": [
            {"theme_name": "Cybersecurity", "secular_score": 85, "tactical_score": 70},
            {"theme_name": "AI Infrastructure", "secular_score": 88, "tactical_score": 45},
            {"theme_name": "Nuclear", "secular_score": 75, "tactical_score": 44},
            {"theme_name": "Energy Security", "secular_score": 73, "tactical_score": 42},
        ],
        "opportunities": [],
        "news_signals": [],
        "macro": [{"symbol": "^VIX", "pct_chg": 39.0}],
        "watchlist": [{"ticker": "QQQ", "pct_chg": -4.8}, {"ticker": "SMH", "pct_chg": -9.2}],
    }


def sector(view: dict, name: str) -> dict:
    return next(row for row in view["sector_views"] if row["sector"] == name)


def test_risk_on_old_regime_neutral_v2_low_confidence_is_fragile():
    view = build_critical_house_view(base_data())

    assert view["market_view"] == "Neutral-to-fragile risk-on"
    assert "low confidence" in view["confidence_interpretation"].lower()


def test_vix_spike_zero_opportunities_adds_caution_warning():
    view = build_critical_house_view(base_data())

    assert any("Volatility and lack" in flag for flag in view["contradiction_flags"])
    assert view["positioning_bias"] == "Selective risk, not broad beta"


def test_high_secular_nuclear_weak_tactical_is_correction_not_broken():
    view = build_critical_house_view(base_data())

    nuclear = sector(view, "Nuclear")
    assert nuclear["critical_view"] == "Long-term bullish, short-term correction"
    assert nuclear["portfolio_bias"] == "Tactical Underweight / Accumulate slowly"


def test_cybersecurity_top_rotation_is_overweight():
    view = build_critical_house_view(base_data())

    cyber = sector(view, "Cybersecurity")
    assert cyber["critical_view"] == "Best risk-adjusted overweight"
    assert cyber["portfolio_bias"] == "Overweight"


def test_semiconductors_strong_secular_drawdown_is_selective_overweight():
    view = build_critical_house_view(base_data())

    semis = sector(view, "Semiconductors")
    assert semis["critical_view"] == "Selective overweight, do not chase rebounds"
    assert semis["portfolio_bias"] == "Selective Overweight"


def test_zero_opportunity_signals_is_cautious_not_empty():
    view = build_critical_house_view(base_data())

    assert "cautious/neutral signal" in view["opportunity_view"]
    assert "Tier 1: Cybersecurity" in view["opportunity_view"]
