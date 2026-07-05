from __future__ import annotations

from db_builder.deepseek_section_generator import (
    SECTION_SPECS,
    assemble_section_report,
    evidence_rows_for_section,
    fallback_section,
    generate_sections,
    section_snapshot,
)


def snapshot() -> dict:
    return {
        "generated_at": "2026-06-14T00:00:00+00:00",
        "window_hours": 24,
        "market_state": {"confidence": 0.3, "volatility_state": "normal", "evidence_id": "REGIME_001"},
        "approved_ticker_universe": ["SPY", "SMH"],
        "approved_etf_label_map": {"SPY": "S&P 500 ETF", "SMH": "Semiconductor ETF"},
        "evidence_appendix": [
            {"evidence_id": "REGIME_001", "section": "market_state", "summary": "risk_on confidence=0.3"},
            {"evidence_id": "CONFIDENCE_001", "section": "confidence", "summary": "moderate-low confidence"},
            {"evidence_id": "MACRO_001", "section": "macro", "summary": "VIX normal"},
            {"evidence_id": "TECH_001", "section": "technicals", "summary": "SPY trend positive"},
            {"evidence_id": "SECTOR_001", "section": "sector", "summary": "Technology ranked high"},
            {"evidence_id": "THEME_001", "section": "theme", "summary": "AI secular support"},
            {"evidence_id": "RISK_001", "section": "risk", "summary": "valuation risk"},
            {"evidence_id": "OPPORTUNITY_001", "section": "opportunity", "summary": "SMH watchlist"},
        ],
    }


def test_section_specific_evidence_filter_works():
    rows = evidence_rows_for_section(snapshot(), ("REGIME", "CONFIDENCE"))

    assert [row["evidence_id"] for row in rows] == ["REGIME_001", "CONFIDENCE_001"]


def test_section_snapshot_contains_only_allowed_evidence():
    spec = SECTION_SPECS[0]
    mini = section_snapshot(snapshot(), spec)

    assert "REGIME_001" in mini["allowed_evidence_ids"]
    assert "MACRO_001" not in mini["allowed_evidence_ids"]


def test_invalid_section_falls_back_without_failing_whole_report(monkeypatch):
    import db_builder.deepseek_section_generator as generator

    monkeypatch.setattr(generator, "retrieve_knowledge", lambda report_task, snapshot: [])
    result = generate_sections(
        snapshot(),
        call_model=True,
        call_section_fn=lambda messages: "## Executive Summary\nUnsupported ETF XOP.",
    )

    assert result["fallback_count"] == len(SECTION_SPECS)
    assert "Fallback used" in result["sections"][0]


def test_model_timeout_falls_back_without_failing_whole_report(monkeypatch):
    import db_builder.deepseek_section_generator as generator

    monkeypatch.setattr(generator, "retrieve_knowledge", lambda report_task, snapshot: [])

    def timeout(_messages):
        raise TimeoutError("timed out")

    result = generate_sections(snapshot(), call_model=True, call_section_fn=timeout)

    assert result["fallback_count"] == len(SECTION_SPECS)
    assert "model call failed" in result["validation_results"][0]["reason"]


def test_final_report_assembles_mixed_valid_and_fallback_sections():
    report = assemble_section_report(
        ["## Executive Summary\nValid [REGIME_001]", fallback_section(SECTION_SPECS[1], snapshot(), reason="bad")],
        [
            {"heading": "## Executive Summary", "valid": True, "reason": ""},
            {"heading": "## Market Bullishness / Bearishness", "valid": False, "reason": "bad"},
        ],
        "## Evidence Appendix\n- [REGIME_001] market_state: risk_on",
    )

    assert "# DeepSeek Offline CIO Report" in report
    assert "## Validation Summary" in report
    assert "fallback_used" in report


def test_section_mode_dry_run_does_not_call_ollama(monkeypatch):
    import db_builder.deepseek_section_generator as generator

    called = {"value": False}

    def call_section(messages):
        called["value"] = True
        return ""

    monkeypatch.setattr(generator, "retrieve_knowledge", lambda report_task, snapshot: [])
    result = generate_sections(snapshot(), call_model=False, call_section_fn=call_section)

    assert called["value"] is False
    assert result["fallback_count"] == len(SECTION_SPECS)


def test_fallback_section_contains_required_heading():
    section = fallback_section(SECTION_SPECS[0], snapshot(), reason="bad")

    assert section.startswith("## Executive Summary")
