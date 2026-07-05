from __future__ import annotations

import pytest

from db_builder.deepseek_stage_outputs import (
    fallback_cross_asset_validation,
    fallback_evidence_extraction,
    fallback_news_weighting,
    fallback_portfolio_construction,
    fallback_regime_scoring,
)
from db_builder.deepseek_stage_validator import (
    consistency_audit,
    extract_json_object,
    validate_final_report,
    validate_stage_output,
)


def snapshot() -> dict:
    return {
        "market_state": {"evidence_id": "REGIME_001", "confidence": 0.3387, "volatility_state": "normal"},
        "approved_ticker_universe": ["SMH", "XLY", "XLP", "SPY", "QQQ"],
        "cross_asset": [
            {"evidence_id": "MACRO_001", "symbol": "^GSPC", "bucket": "equities", "pct_chg": 1.0},
            {"evidence_id": "MACRO_002", "symbol": "^TNX", "bucket": "rates", "pct_chg": 1.0},
        ],
        "news": [{"evidence_id": "NEWS_001", "title": "SpaceX IPO hype", "themes": ["IPO"]}],
        "sector_data": [
            {"evidence_id": "SECTOR_001", "sector": "Semiconductors", "allocation_bias": "overweight", "related_etfs": ["SMH"]},
            {"evidence_id": "SECTOR_002", "sector": "Consumer Staples", "allocation_bias": "overweight", "related_etfs": ["XLP"]},
        ],
        "evidence_appendix": [
            {"evidence_id": "REGIME_001", "section": "market", "summary": "confidence=0.3387"},
            {"evidence_id": "MACRO_001", "section": "macro", "summary": "equities up"},
            {"evidence_id": "MACRO_002", "section": "macro", "summary": "rates up"},
            {"evidence_id": "NEWS_001", "section": "news", "summary": "SpaceX IPO hype"},
            {"evidence_id": "SECTOR_001", "section": "sector", "summary": "Semiconductors"},
            {"evidence_id": "SECTOR_002", "section": "sector", "summary": "Consumer Staples"},
        ],
    }


def valid_final_markdown(scenario_rows: str, extra_executive: str = "Text") -> str:
    return f"""# Qwen Multi-Stage CIO Report

## Executive Summary
{extra_executive}
## Macro Market Dashboard
Text
## Macro Regime Classification
Text
## News Analysis
- NEWS_001: SpaceX IPO hype
## Evidence Table
Evidence ID | Meaning
## Cross-Asset Confirmation
Text
## Market Narrative
Text
## Scenario Analysis
{scenario_rows}
## Sector Analysis
Text
## Risk Register
Text
## Contradictions
Text
## Investment Recommendations
Text
## Invalidators
Text
## Confidence Assessment
Text
"""


def test_extract_json_object_from_wrapped_text():
    assert extract_json_object("prefix {\"stage\":\"x\"} suffix") == {"stage": "x"}


def test_stage_2_probability_validation():
    payload = fallback_regime_scoring(snapshot())
    payload["scenario_probabilities"]["risk_on_expansion"] += 1

    with pytest.raises(ValueError, match="sum to 100"):
        validate_stage_output("regime_scoring", payload, snapshot())


def test_stage_3_identifies_cross_asset_contradiction():
    payload = fallback_cross_asset_validation(snapshot())

    assert validate_stage_output("cross_asset_validation", payload, snapshot())["asset_class_tests"]


def test_stage_validation_allows_macro_acronyms_like_opec():
    payload = fallback_cross_asset_validation(snapshot())
    payload["asset_class_tests"][0]["signal"] = "Oil is affected by OPEC supply discipline."

    assert validate_stage_output("cross_asset_validation", payload, snapshot())["asset_class_tests"]


def test_stage_4_prevents_noise_top_systemic_risk():
    payload = fallback_news_weighting(snapshot())
    payload["top_systemic_risks"] = [{"risk": "SpaceX", "evidence_ids": ["NEWS_001"], "why_it_matters": "noise"}]

    with pytest.raises(ValueError, match="Narrative noise"):
        validate_stage_output("news_weighting", payload, snapshot())


def test_stage_4_accepts_dict_shaped_narrative_noise():
    payload = fallback_news_weighting(snapshot())
    payload["narrative_noise"] = [{"evidence_id": "NEWS_001", "reason": "speculative media"}]
    payload["top_systemic_risks"] = [{"risk": "macro", "evidence_ids": ["MACRO_001"], "why_it_matters": "macro"}]

    assert validate_stage_output("news_weighting", payload, snapshot())["narrative_noise"]


def test_stage_4_accepts_singular_risk_evidence_id():
    payload = fallback_news_weighting(snapshot())
    payload["top_systemic_risks"] = [{"risk": "macro", "evidence_id": "MACRO_001", "why_it_matters": "macro"}]
    payload["narrative_noise"] = []

    assert validate_stage_output("news_weighting", payload, snapshot())["top_systemic_risks"]


def test_stage_4_rejects_top_risk_without_evidence_id():
    payload = fallback_news_weighting(snapshot())
    payload["top_systemic_risks"] = [{"risk": "macro", "why_it_matters": "macro"}]

    with pytest.raises(ValueError, match="Every top systemic risk needs evidence IDs"):
        validate_stage_output("news_weighting", payload, snapshot())


def test_stage_5_rejects_invented_evidence_id():
    payload = fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot()))
    payload["sector_recommendations"][0]["evidence_ids"] = ["EVIDENCE_015"]

    with pytest.raises(ValueError, match="Unsupported evidence ID"):
        validate_stage_output("portfolio_construction", payload, snapshot())


def test_stage_5_prevents_same_etf_buy_and_reduce():
    payload = fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot()))
    payload["tactical_buy_list"] = [{"asset": "SMH", "action": "hold", "reason": "x", "evidence_ids": ["SECTOR_001"]}]
    payload["reduce_list"] = [{"asset": "SMH", "action": "trim", "reason": "x", "evidence_ids": ["SECTOR_001"]}]

    with pytest.raises(ValueError, match="Same asset"):
        validate_stage_output("portfolio_construction", payload, snapshot())


def test_stage_5_uses_snapshot_confidence_to_reject_aggressive_overweight():
    payload = fallback_portfolio_construction(snapshot(), {"confidence": 80})
    payload["overall_stance"] = "aggressive_risk_on"
    payload["sector_recommendations"][0]["bias"] = "OW"
    payload["sector_recommendations"][0]["sizing"] = "core_overweight"
    payload["confidence_constraints"] = []

    with pytest.raises(ValueError, match="Low confidence"):
        validate_stage_output("portfolio_construction", payload, snapshot())


def test_stage_5_accepts_structured_confidence_constraints():
    payload = fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot()))
    payload["confidence_constraints"] = [{"reason": "low confidence"}]

    assert validate_stage_output("portfolio_construction", payload, snapshot())["confidence_constraints"]


def test_final_report_uses_stage_probabilities():
    stage_outputs = {
        "regime_scoring": fallback_regime_scoring(snapshot()),
        "cross_asset_validation": fallback_cross_asset_validation(snapshot()),
        "news_weighting": fallback_news_weighting(snapshot()),
        "portfolio_construction": fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot())),
    }
    probs = stage_outputs["regime_scoring"]["scenario_probabilities"]
    rows = "\n".join(f"| {key} | {value} |" for key, value in probs.items())
    markdown = valid_final_markdown(rows)

    assert validate_final_report(markdown, stage_outputs, snapshot()).startswith("# Qwen")


def test_final_report_rejects_unsupported_number():
    stage_outputs = {
        "regime_scoring": fallback_regime_scoring(snapshot()),
        "cross_asset_validation": fallback_cross_asset_validation(snapshot()),
        "news_weighting": fallback_news_weighting(snapshot()),
        "portfolio_construction": fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot())),
    }
    probs = stage_outputs["regime_scoring"]["scenario_probabilities"]
    rows = "\n".join(f"| {key} | {value} |" for key, value in probs.items())
    markdown = valid_final_markdown(rows, extra_executive="Invented price target 9999.")

    with pytest.raises(ValueError, match="Unsupported numeric value"):
        validate_final_report(markdown, stage_outputs, snapshot())


def test_final_report_locks_mismatched_scenario_table_to_stage_2():
    stage_outputs = {
        "regime_scoring": fallback_regime_scoring(snapshot()),
        "cross_asset_validation": fallback_cross_asset_validation(snapshot()),
        "news_weighting": fallback_news_weighting(snapshot()),
        "portfolio_construction": fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot())),
    }
    probs = stage_outputs["regime_scoring"]["scenario_probabilities"]
    markdown = valid_final_markdown(
        """| Scenario | Probability | Source |
|---|---:|---|
| Base Case | 99 | bad |
| Bull Case | 1 | bad |"""
    )

    validated = validate_final_report(markdown, stage_outputs, snapshot())

    bull = int(probs.get("risk_on_expansion") or 0)
    bear = int(probs.get("defensive_slowdown") or 0) + int(probs.get("risk_off_shock") or 0)
    base = max(0, 100 - bull - bear)
    assert f"| Base Case | {base} |" in validated
    assert f"| Bull Case | {bull} |" in validated
    assert f"| Bear Case | {bear} |" in validated


def test_final_report_replaces_macro_dashboard_with_snapshot_values():
    stage_outputs = {
        "regime_scoring": fallback_regime_scoring(snapshot()),
        "cross_asset_validation": fallback_cross_asset_validation(snapshot()),
        "news_weighting": fallback_news_weighting(snapshot()),
        "portfolio_construction": fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot())),
    }
    markdown = valid_final_markdown(
        """| Scenario | Probability | Source |
|---|---:|---|
| Base Case | 99 | bad |
| Bull Case | 1 | bad |""",
    ).replace("## Macro Market Dashboard\nText", "## Macro Market Dashboard\nGeneric macro summary.")

    validated = validate_final_report(markdown, stage_outputs, snapshot())

    assert "S&P 500: up / strong; close=close unavailable, pct_chg=1.0" in validated
    assert "Generic macro summary" not in validated


def test_final_report_allows_evidence_id_label_text():
    stage_outputs = {
        "regime_scoring": fallback_regime_scoring(snapshot()),
        "cross_asset_validation": fallback_cross_asset_validation(snapshot()),
        "news_weighting": fallback_news_weighting(snapshot()),
        "portfolio_construction": fallback_portfolio_construction(snapshot(), fallback_regime_scoring(snapshot())),
    }
    probs = stage_outputs["regime_scoring"]["scenario_probabilities"]
    rows = "\n".join(f"| {key} | {value} |" for key, value in probs.items())
    markdown = valid_final_markdown(rows, extra_executive="Evidence ID references are required.")

    assert validate_final_report(markdown, stage_outputs, snapshot()).startswith("# Qwen")


def test_final_audit_flags_low_confidence_aggressive_allocation():
    outputs = {
        "regime_scoring": {"confidence": 30},
        "portfolio_construction": {"overall_stance": "aggressive_risk_on", "sector_recommendations": []},
        "news_weighting": {"narrative_noise": [], "top_systemic_risks": []},
    }

    warnings = consistency_audit(outputs, "clean risk-on")

    assert any(warning["severity"] == "serious" for warning in warnings)
