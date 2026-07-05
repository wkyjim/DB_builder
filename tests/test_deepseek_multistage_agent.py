from __future__ import annotations

from db_builder.deepseek_multistage_agent import (
    DEFAULT_MULTISTAGE_MODEL,
    OllamaSettings,
    _final_report_repair_messages,
    enrich_news_weighting,
    run_multistage_report,
    run_stage,
)
from db_builder.deepseek_stage_prompts import FINAL_REPORT_HEADINGS
from db_builder.deepseek_stage_outputs import fallback_final_report
from db_builder.deepseek_stage_outputs import (
    fallback_cross_asset_validation,
    fallback_news_weighting,
    fallback_portfolio_construction,
    fallback_regime_scoring,
)


class Engine:
    pass


def snapshot() -> dict:
    return {
        "generated_at": "2026-06-16T00:00:00+00:00",
        "window_hours": 24,
        "market_state": {"evidence_id": "REGIME_001", "confidence": 0.3387, "risk_on_score": 65, "risk_off_score": 35},
        "cross_asset": [
            {"evidence_id": "MACRO_001", "symbol": "^GSPC", "bucket": "equities", "pct_chg": 1.0},
            {"evidence_id": "MACRO_002", "symbol": "^TNX", "bucket": "rates", "pct_chg": 1.0},
        ],
        "technicals": [{"evidence_id": "TECH_001", "ticker": "SMH", "return_20d": 10.0, "close": 100, "rsi": 60}],
        "news": [{"evidence_id": "NEWS_001", "title": "SpaceX IPO hype", "themes": ["IPO"]}],
        "sector_data": [{"evidence_id": "SECTOR_001", "sector": "Semiconductors", "allocation_bias": "overweight", "related_etfs": ["SMH"], "regime": "bull", "rotation_rank": 1}],
        "secular_themes": [{"evidence_id": "THEME_001", "theme": "AI Infrastructure", "related_etfs": ["SMH"]}],
        "opportunity_risk": {"top_opportunities": [], "top_risks": [], "no_opportunity_warning": "none"},
        "confidence_inputs": [{"evidence_id": "CONFIDENCE_001", "input": "confidence", "value": 0.3387}],
        "approved_ticker_universe": ["SMH", "SPY", "^GSPC", "^TNX"],
        "approved_etf_label_map": {"SMH": "Semiconductor ETF"},
        "unavailable_fields": {},
        "evidence_appendix": [
            {"evidence_id": "REGIME_001", "section": "market", "summary": "confidence=0.3387"},
            {"evidence_id": "MACRO_001", "section": "macro", "summary": "equities up"},
            {"evidence_id": "MACRO_002", "section": "macro", "summary": "rates up"},
            {"evidence_id": "TECH_001", "section": "tech", "summary": "SMH up"},
            {"evidence_id": "NEWS_001", "section": "news", "summary": "SpaceX IPO hype"},
            {"evidence_id": "SECTOR_001", "section": "sector", "summary": "Semiconductors"},
            {"evidence_id": "THEME_001", "section": "theme", "summary": "AI"},
            {"evidence_id": "CONFIDENCE_001", "section": "confidence", "summary": "low"},
        ],
    }


def test_cli_dry_run_does_not_call_ollama(monkeypatch):
    import db_builder.deepseek_multistage_agent as agent

    called = {"value": False}
    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: snapshot())

    def chat_fn(**kwargs):
        called["value"] = True
        return ""

    result = run_multistage_report(Engine(), call_model=False, chat_fn=chat_fn)

    assert result["status"] == "preview"
    assert called["value"] is False


def test_default_model_is_qwen_14b():
    assert OllamaSettings().model == DEFAULT_MULTISTAGE_MODEL == "qwen2.5:14b"


def test_call_model_runs_stages_in_order_with_fallbacks(monkeypatch):
    import db_builder.deepseek_multistage_agent as agent

    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: snapshot())
    seen = []

    def chat_fn(**kwargs):
        content = kwargs["messages"][1]["content"]
        for stage in ["evidence_extraction", "regime_scoring", "cross_asset_validation", "news_weighting", "portfolio_construction", "final_cio_report"]:
            if stage in content:
                seen.append(stage)
                break
        raise TimeoutError("force fallback")

    result = run_multistage_report(Engine(), call_model=True, settings=OllamaSettings(stage_timeout=1), chat_fn=chat_fn)

    assert [stage["stage"] for stage in result["stage_results"]] == [
        "evidence_extraction",
        "regime_scoring",
        "cross_asset_validation",
        "news_weighting",
        "portfolio_construction",
        "final_cio_report",
    ]
    assert result["markdown"]


def test_global_timeout_budget_falls_back_before_calling_model(monkeypatch):
    import db_builder.deepseek_multistage_agent as agent

    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: snapshot())
    times = iter([0, 2, 3, 4, 5, 6, 7])
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(times))
    called = {"value": False}

    def chat_fn(**kwargs):
        called["value"] = True
        return "{}"

    result = run_multistage_report(
        Engine(),
        call_model=True,
        settings=OllamaSettings(timeout=1, stage_timeout=10),
        chat_fn=chat_fn,
    )

    assert called["value"] is False
    assert result["stage_results"][0]["status"] == "fallback_used"
    assert "Global time budget" in result["stage_results"][0]["reason"]


def test_zero_timeout_disables_global_budget(monkeypatch):
    import db_builder.deepseek_multistage_agent as agent

    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: snapshot())
    monkeypatch.setattr(agent.time, "monotonic", lambda: 999999)
    calls = {"count": 0}

    def chat_fn(**kwargs):
        calls["count"] += 1
        raise TimeoutError("force fallback")

    result = run_multistage_report(
        Engine(),
        call_model=True,
        settings=OllamaSettings(timeout=0, stage_timeout=0),
        chat_fn=chat_fn,
    )

    assert calls["count"] > 0
    assert result["stage_results"][0]["reason"] != "Global time budget reached before stage."


def test_news_weighting_enrichment_replaces_placeholder_headline_fields():
    payload = {
        "stage": "news_weighting",
        "headline_classifications": [
            {
                "evidence_id": "NEWS_001",
                "title": "headline title from snapshot",
                "source": "source name from snapshot",
                "category": "systemic_macro",
            }
        ],
    }

    enriched = enrich_news_weighting(payload, snapshot())

    assert enriched["headline_classifications"][0]["title"] == "SpaceX IPO hype"
    assert enriched["headline_classifications"][0]["source"] is None


def test_final_report_repair_prompt_repeats_required_headings():
    messages = _final_report_repair_messages([], "# Qwen", "Missing final report headings")
    content = messages[-1]["content"]

    for heading in FINAL_REPORT_HEADINGS:
        assert heading in content


def test_diagnostics_output_works_when_invalid(monkeypatch):
    import db_builder.deepseek_multistage_agent as agent

    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: snapshot())
    result = run_multistage_report(Engine(), call_model=True, include_stage_json=True, chat_fn=lambda **kwargs: (_ for _ in ()).throw(TimeoutError("x")))

    assert "## Validation Summary" in result["markdown"]
    assert "## Stage JSON" in result["markdown"]


def test_final_report_retry_repairs_unsupported_number():
    regime = fallback_regime_scoring(snapshot())
    stage_outputs = {
        "regime_scoring": regime,
        "cross_asset_validation": fallback_cross_asset_validation(snapshot(), regime),
        "news_weighting": fallback_news_weighting(snapshot()),
        "portfolio_construction": fallback_portfolio_construction(snapshot(), regime),
    }
    probs = regime["scenario_probabilities"]
    rows = "\n".join(f"| {key} | {value} |" for key, value in probs.items())
    invalid = f"""# Qwen Multi-Stage CIO Report

## Executive Summary
Invented exposure range 9999.
## Macro Market Dashboard
Text
## Macro Regime Classification
Text
## News Analysis
- NEWS_001: SpaceX IPO hype
## Evidence Table
Text
## Cross-Asset Confirmation
Text
## Market Narrative
Text
## Scenario Analysis
{rows}
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
    valid = f"""# Qwen Multi-Stage CIO Report

## Executive Summary
Selective stance with qualitative risk control.
## Macro Market Dashboard
Text
## Macro Regime Classification
Text
## News Analysis
- NEWS_001: SpaceX IPO hype
## Evidence Table
Text
## Cross-Asset Confirmation
Text
## Market Narrative
Text
## Scenario Analysis
{rows}
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
    responses = iter([invalid, valid])
    seen_repair = {"value": False}

    def chat_fn(**kwargs):
        messages = kwargs["messages"]
        if "VALIDATION ERROR" in messages[-1]["content"]:
            seen_repair["value"] = True
        return {"message": {"content": next(responses)}}

    result = run_stage(
        "final_cio_report",
        snapshot(),
        stage_outputs,
        call_model=True,
        settings=OllamaSettings(),
        chat_fn=chat_fn,
    )

    assert result["status"] == "valid_qwen"
    assert seen_repair["value"] is True


def test_fallback_final_report_contains_required_sections():
    report = fallback_final_report(
        {
            "regime_scoring": {"dominant_regime": "soft_landing", "confidence": 35, "scenario_probabilities": {"risk_on_expansion": 20, "soft_landing": 30, "growth_plus_inflation": 20, "defensive_slowdown": 20, "risk_off_shock": 10}},
            "cross_asset_validation": {"summary": "mixed", "key_contradictions": []},
            "news_weighting": {
                "headline_classifications": [
                    {
                        "evidence_id": "NEWS_001",
                        "title": "SpaceX IPO hype",
                        "source": "Test Source",
                        "category": "speculative_media",
                        "reason": "low systemic relevance",
                    }
                ],
                "top_systemic_risks": [],
                "risk_ranking_summary": "noise separated",
            },
            "portfolio_construction": {"overall_stance": "selective_risk", "sector_recommendations": [], "tactical_buy_list": [], "reduce_list": []},
        },
        snapshot(),
    )

    assert "# Qwen Multi-Stage CIO Report" in report
    assert "## News Analysis" in report
    assert "## Macro Market Dashboard" in report
    assert "SpaceX IPO hype" in report
    assert "## Confidence Assessment" in report
