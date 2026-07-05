from __future__ import annotations

from db_builder.deepseek_stage_prompts import STAGE_ORDER, build_stage_prompt, prompts_preview


def snapshot() -> dict:
    return {
        "market_state": {"evidence_id": "REGIME_001"},
        "cross_asset": [{"evidence_id": "MACRO_001", "symbol": "GC=F"}],
        "news": [{"evidence_id": "NEWS_001", "title": "Inflation risk"}],
        "sector_data": [{"evidence_id": "SECTOR_001", "sector": "Semiconductors"}],
        "technicals": [{"evidence_id": "TECH_001", "ticker": "SMH"}],
        "evidence_appendix": [
            {"evidence_id": "REGIME_001", "section": "market_state", "summary": "risk_on"},
            {"evidence_id": "MACRO_001", "section": "macro", "summary": "gold"},
            {"evidence_id": "NEWS_001", "section": "news", "summary": "inflation"},
            {"evidence_id": "SECTOR_001", "section": "sector", "summary": "semis"},
            {"evidence_id": "TECH_001", "section": "tech", "summary": "SMH"},
        ],
    }


def test_stage_prompt_contains_schema_for_json_stage():
    messages = build_stage_prompt("regime_scoring", snapshot(), {"evidence_extraction": {"stage": "evidence_extraction"}})
    combined = "\n".join(message["content"] for message in messages)

    assert "STRICT JSON SCHEMA" in combined
    assert '"stage": "regime_scoring"' in combined


def test_stage_prompt_lists_allowed_evidence_ids():
    messages = build_stage_prompt("news_weighting", snapshot(), {"evidence_extraction": {"stage": "evidence_extraction"}})
    combined = "\n".join(message["content"] for message in messages)

    assert "ALLOWED EVIDENCE IDS" in combined
    assert "NEWS_001" in combined
    assert "Never invent IDs such as EVIDENCE_001" in combined
    assert '"evidence_ids": [\n        "NEWS_001"\n      ]' in combined


def test_final_prompt_contains_required_headings():
    messages = build_stage_prompt(
        "final_cio_report",
        snapshot(),
        {"regime_scoring": {"stage": "regime_scoring", "scenario_probabilities": {"risk_on_expansion": 50, "soft_landing": 30}}},
    )
    combined = "\n".join(message["content"] for message in messages)

    assert "# Qwen Multi-Stage CIO Report" in combined
    assert "## Macro Market Dashboard" in combined
    assert "## News Analysis" in combined
    assert "## Investment Recommendations" in combined
    assert "## Confidence Assessment" in combined
    assert "TOP NEWS HEADLINES FROM SNAPSHOT" in combined
    assert "Inflation risk" in combined
    assert "## Scenario Analysis" in combined
    assert "LOCKED BASE/BULL/BEAR SCENARIO TABLE" in combined
    assert "| Bull Case | 50 | risk_on_expansion |" in combined
    assert "copy the locked scenario table exactly" in combined


def test_prompts_preview_lists_stages():
    prompts = {stage: build_stage_prompt(stage, snapshot(), {}) for stage in STAGE_ORDER[:2]}

    preview = prompts_preview(prompts)

    assert "Stage 1: Evidence Extraction" in preview
    assert "Stage 2: Regime Scoring" in preview
