from __future__ import annotations

import pytest

from db_builder.deepseek_report_validator import validate_report, validate_section


def snapshot() -> dict:
    return {
        "market_state": {
            "market_regime": "risk_on",
            "confidence": 0.3,
            "volatility_state": "normal",
            "evidence_id": "REGIME_001",
        },
        "approved_ticker_universe": ["SPY", "XLE", "CIBR", "IWM", "^VIX", "NQ=F"],
        "approved_etf_label_map": {"SPY": "S&P 500 ETF", "XLE": "Energy ETF", "CIBR": "Cybersecurity ETF"},
        "cross_asset": [{"symbol": "^VIX", "close": 18.92, "evidence_id": "MACRO_001"}],
        "evidence_appendix": [
            {"evidence_id": "REGIME_001", "section": "market_state", "summary": "risk_on"},
            {"evidence_id": "MACRO_001", "section": "cross_asset", "summary": "VIX close=18.92"},
        ],
    }


VALID_REPORT = """# DeepSeek CIO House View

## Executive Summary
Market is selective. [REGIME_001]

## Market Bullishness / Bearishness
Bullish but not clean risk-on. [REGIME_001]

## Investor Sentiment
Volatility evidence is available. [MACRO_001]

## Cross-Asset Confirmation
Cross-asset confirmation is mixed. [MACRO_001]

## Sector Strength Ranking
S&P 500 ETF (SPY) is a beta reference. [REGIME_001]

## Highest Conviction Opportunities
No single-name opportunities are assumed. [REGIME_001]

## Tactical Positioning
Positioning should remain selective. [REGIME_001]

## Tactical Buy List
Buy list remains thematic. [REGIME_001]

## Tactical Sell / Reduce List
Reduce unsupported risk. [REGIME_001]

## Key Risks
Risk is confidence-sensitive. [REGIME_001]

## Risk Management
Manage exposure by confidence. [REGIME_001]

## Alternative Scenario
The view changes if evidence changes. [REGIME_001]

## Final House View
Selective risk is preferred. [REGIME_001]

## Evidence Appendix
- [REGIME_001] market_state: risk_on
- [MACRO_001] cross_asset: VIX close=18.92
"""


def test_validator_accepts_valid_report():
    assert validate_report(VALID_REPORT, snapshot()).startswith("# DeepSeek CIO House View")


def test_validator_rejects_missing_headings():
    with pytest.raises(ValueError, match="Missing required headings"):
        validate_report("# DeepSeek CIO House View\n\n## Executive Summary\nText [REGIME_001]", snapshot())


def test_validator_rejects_unsupported_number():
    bad = VALID_REPORT.replace("Selective risk is preferred.", "Unsupported level 12345.")

    with pytest.raises(ValueError, match="Unsupported number"):
        validate_report(bad, snapshot())


def test_validator_rejects_unsupported_ticker():
    bad = VALID_REPORT.replace("Selective risk is preferred.", "Unsupported ETF XOP.")

    with pytest.raises(ValueError, match="Unsupported ticker"):
        validate_report(bad, snapshot())


def test_validator_allows_evidence_id_phrase():
    report = VALID_REPORT.replace("Selective risk is preferred.", "Evidence ID format is documented. [REGIME_001]")

    assert validate_report(report, snapshot()).startswith("# DeepSeek CIO House View")


def test_validator_allows_harmless_labels_and_market_terms():
    report = VALID_REPORT.replace(
        "Selective risk is preferred.",
        "The Cybersecurity ETF (CIBR), and Cybersecurity ETF (CIBR), IWM-SPY relative trend, NQ futures, CAC, ESG, FOMO, and MACD are referenced. [REGIME_001]",
    )

    assert validate_report(report, snapshot()).startswith("# DeepSeek CIO House View")


def test_validator_rejects_unsupported_evidence_id():
    bad = VALID_REPORT.replace("[REGIME_001]", "[FAKE_001]", 1)

    with pytest.raises(ValueError, match="Unsupported evidence ID"):
        validate_report(bad, snapshot())


def test_validator_rejects_missing_evidence_ids():
    bad = VALID_REPORT.replace("Market is selective. [REGIME_001]", "Market is selective.")

    with pytest.raises(ValueError, match="lacks evidence IDs"):
        validate_report(bad, snapshot())


def test_validator_rejects_clean_risk_on_with_low_confidence():
    low_conf = snapshot()
    low_conf["market_state"]["confidence"] = 0.1
    bad = VALID_REPORT.replace("Selective risk is preferred.", "Clean risk-on is preferred. [REGIME_001]")

    with pytest.raises(ValueError, match="clean risk-on"):
        validate_report(bad, low_conf)


def test_section_validator_rejects_unsupported_evidence_id():
    section = "## Executive Summary\nMarket is selective. [FAKE_001]"

    with pytest.raises(ValueError, match="Unsupported evidence ID"):
        validate_section(
            section,
            snapshot(),
            section_heading="## Executive Summary",
            allowed_evidence_ids={"REGIME_001"},
        )


def test_section_validator_rejects_unsupported_ticker():
    section = "## Executive Summary\nUnsupported ETF XOP. [REGIME_001]"

    with pytest.raises(ValueError, match="Unsupported ticker"):
        validate_section(
            section,
            snapshot(),
            section_heading="## Executive Summary",
            allowed_evidence_ids={"REGIME_001"},
        )


def test_section_validator_rejects_unsupported_number():
    section = "## Executive Summary\nUnsupported level 12345. [REGIME_001]"

    with pytest.raises(ValueError, match="Unsupported number"):
        validate_section(
            section,
            snapshot(),
            section_heading="## Executive Summary",
            allowed_evidence_ids={"REGIME_001"},
        )


def test_section_validator_allows_decimal_zero_format():
    section = "## Executive Summary\nRisk score is 33.0. [REGIME_001]"
    data = snapshot()
    data["market_state"]["risk_score"] = 33.0

    assert validate_section(
        section,
        data,
        section_heading="## Executive Summary",
        allowed_evidence_ids={"REGIME_001"},
    ).startswith("## Executive Summary")


def test_section_validator_allows_numbers_from_evidence_strings_and_percent_conversion():
    section = "## Executive Summary\nConfidence is 33.87 and risk driver is 40. [REGIME_001]"
    data = snapshot()
    data["market_state"]["confidence"] = 0.3387
    data["market_state"]["drivers"] = {"news": ["Geopolitics opp=40 risk=49.98"]}

    assert validate_section(
        section,
        data,
        section_heading="## Executive Summary",
        allowed_evidence_ids={"REGIME_001"},
    ).startswith("## Executive Summary")
