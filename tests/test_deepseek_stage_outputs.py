from __future__ import annotations

from db_builder.deepseek_stage_outputs import (
    fallback_cross_asset_validation,
    fallback_evidence_extraction,
    fallback_final_report,
    fallback_news_weighting,
    fallback_portfolio_construction,
    fallback_regime_scoring,
    macro_dashboard_lines,
)


def snapshot() -> dict:
    return {
        "market_state": {"evidence_id": "REGIME_001", "confidence": 0.3387, "risk_on_score": 65, "risk_off_score": 35},
        "cross_asset": [
            {"evidence_id": "MACRO_001", "symbol": "GC=F", "bucket": "commodities", "pct_chg": 3.0, "close": 2400, "date": "2026-06-29"},
            {"evidence_id": "MACRO_002", "symbol": "^GSPC", "bucket": "equities", "pct_chg": -0.2, "close": 6100, "date": "2026-06-29"},
            {"evidence_id": "MACRO_003", "symbol": "^FVX", "bucket": "rates", "pct_chg": -1.1, "close": 4.0, "date": "2026-06-29"},
            {"evidence_id": "MACRO_004", "symbol": "^TNX", "bucket": "rates", "pct_chg": -0.8, "close": 4.2, "date": "2026-06-29"},
            {"evidence_id": "MACRO_005", "symbol": "^TYX", "bucket": "rates", "pct_chg": -0.4, "close": 4.7, "date": "2026-06-29"},
            {"evidence_id": "MACRO_006", "symbol": "^VIX", "bucket": "volatility", "pct_chg": 2.5, "close": 18, "date": "2026-06-29"},
        ],
        "technicals": [{"evidence_id": "TECH_001", "ticker": "SMH", "return_20d": 10.0, "close": 100, "rsi": 60, "pct_chg": 1.2}],
        "sector_data": [
            {"evidence_id": "SECTOR_001", "sector": "Semiconductors", "allocation_bias": "overweight", "related_etfs": ["SMH"], "regime": "bull", "rotation_rank": 1},
            {"evidence_id": "SECTOR_002", "sector": "Consumer Discretionary", "allocation_bias": "underweight", "related_etfs": ["XLY"], "regime": "bear", "rotation_rank": 12},
        ],
        "news": [
            {"evidence_id": "NEWS_001", "title": "SpaceX IPO hype is massive", "themes": ["IPO"]},
            {"evidence_id": "NEWS_002", "title": "Inflation pressure hits market", "themes": ["inflation"]},
        ],
        "unavailable_fields": {"fund_flows": None},
    }


def test_stage_1_extracts_facts_without_recommendations():
    output = fallback_evidence_extraction(snapshot())

    assert output["stage"] == "evidence_extraction"
    assert output["facts"]
    assert all("overweight" not in fact["signal"].lower() for fact in output["facts"])


def test_stage_2_probabilities_sum_to_100():
    output = fallback_regime_scoring(snapshot(), fallback_evidence_extraction(snapshot()))

    assert sum(output["scenario_probabilities"].values()) == 100


def test_stage_4_classifies_spacex_as_speculative_media():
    output = fallback_news_weighting(snapshot())

    spacex = next(row for row in output["headline_classifications"] if row["evidence_id"] == "NEWS_001")
    assert spacex["category"] == "speculative_media"


def test_stage_5_prevents_aggressive_overweight_when_confidence_low():
    stage2 = fallback_regime_scoring(snapshot())
    output = fallback_portfolio_construction(snapshot(), stage2)

    assert output["overall_stance"] != "aggressive_risk_on"
    assert all(row["bias"] != "OW" for row in output["sector_recommendations"])


def test_stage_5_snapshot_confidence_caps_model_confidence():
    output = fallback_portfolio_construction(snapshot(), {"confidence": 80})

    assert output["overall_stance"] == "selective_risk"
    assert all(row["sizing"] != "core_overweight" for row in output["sector_recommendations"])


def test_macro_dashboard_lines_render_snapshot_values():
    lines = "\n".join(macro_dashboard_lines(snapshot()))

    assert "S&P 500: stable / flat; close=6100" in lines
    assert "5Y Treasury: down / strong; close=4.0" in lines
    assert "Gold: up / strong; close=2400" in lines
    assert "VIX: up / strong; close=18" in lines
    assert "[MACRO_001]" in lines


def test_fallback_final_report_uses_snapshot_macro_dashboard():
    snap = snapshot()
    stage1 = fallback_evidence_extraction(snap)
    stage2 = fallback_regime_scoring(snap, stage1)
    stage3 = fallback_cross_asset_validation(snap, stage2)
    stage4 = fallback_news_weighting(snap, stage3)
    stage5 = fallback_portfolio_construction(snap, stage2, stage3, stage4)
    report = fallback_final_report(
        {
            "evidence_extraction": stage1,
            "regime_scoring": stage2,
            "cross_asset_validation": stage3,
            "news_weighting": stage4,
            "portfolio_construction": stage5,
        },
        snap,
    )

    assert "S&P 500: stable / flat; close=6100" in report
    assert "use available macro and technical evidence" not in report
