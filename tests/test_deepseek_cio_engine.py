from __future__ import annotations

from datetime import datetime, timezone

import pytest

from db_builder.deepseek_cio_engine import (
    build_cio_prompt,
    build_cio_snapshot_from_data,
    fallback_cio_report,
    framework_path,
    generate_cio_report,
    load_cio_framework,
    save_deepseek_cio_report,
    validate_cio_markdown,
)


GENERATED_AT = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def sample_data() -> dict:
    return {
        "generated_at": GENERATED_AT,
        "window_hours": 24,
        "regime": [{"regime_label": "risk_on", "confidence_score": 0.3}],
        "regime_v2": [
            {
                "market_regime": "risk_on",
                "market_phase": "mid_bull",
                "confidence": 0.29,
                "technical_score": 72,
                "momentum_score": 68,
                "breadth_score": 63,
                "risk_appetite_score": 58,
                "news_score": 60,
                "market_strength": "moderate",
                "trend_state": "uptrend",
                "momentum_state": "stable_positive",
                "volatility_state": "normal",
                "breadth_state": "healthy",
                "risk_appetite_state": "neutral",
            }
        ],
        "sector_rotation": [
            {
                "sector_name": "Semiconductors",
                "related_etfs": ["SMH", "SOXX"],
                "rotation_rank": 1,
                "rotation_score": 76,
                "allocation_bias": "overweight",
                "recommended_action": "add",
            },
            {
                "sector_name": "Utilities",
                "related_etfs": ["XLU"],
                "rotation_rank": 13,
                "rotation_score": 28,
                "allocation_bias": "underweight",
                "recommended_action": "trim",
            },
        ],
        "sector_regimes": [
            {
                "sector_name": "Semiconductors",
                "related_etfs": ["SMH", "SOXX"],
                "sector_regime": "bull",
                "cycle_phase": "mid_bull",
                "confidence": 0.45,
                "final_score": 74,
            }
        ],
        "secular_themes": [
            {
                "theme_name": "AI Infrastructure",
                "parent_theme": "AI & Compute",
                "secular_score": 84,
                "tactical_score": 62,
                "theme_phase": "secular_bull",
                "confidence": 0.72,
                "related_etfs": ["QQQ", "SMH"],
            }
        ],
        "news_signals": [
            {
                "dimension_type": "theme",
                "dimension_value": "AI",
                "article_count": 8,
                "opportunity_score": 75,
                "risk_score": 24,
                "top_article_ids": ["00000000-0000-0000-0000-000000000001"],
            }
        ],
        "opportunities": [],
        "articles": [
            {
                "article_id": "00000000-0000-0000-0000-000000000001",
                "title": "AI infrastructure spending accelerates",
                "source_name": "CNBC",
                "source_priority": 95,
                "source_category": "technology",
                "impact_score": 80,
                "confidence_score": 0.8,
                "themes": ["AI"],
                "affected_tickers": ["SMH"],
            }
        ],
        "macro": [{"symbol": "^VIX", "name": "VIX", "close": 17, "pct_chg": -3.0}],
        "watchlist": [{"ticker": "SMH", "close": 260, "pct_chg": 1.4, "rsi_14": 58, "return_20d": 4.2}],
    }


VALID_MARKDOWN = """# DeepSeek CIO House View

## Executive Summary

Summary. [REGIME_001]

## Market Bullishness / Bearishness

Assessment. [REGIME_001]

## Investor Sentiment

Sentiment. [MACRO_001]

## Sector Strength Ranking

Ranking. [SECTOR_ROTATION_001]

## Highest Conviction Opportunities

Opportunities. [OPPORTUNITY_001]

## Tactical Positioning

Positioning. [SECTOR_ROTATION_001]

## Tactical Buy List

Buy list. [SECTOR_ROTATION_001]

## Tactical Sell / Reduce List

Reduce list. [SECTOR_ROTATION_002]

## Key Risks

Risks. [RISK_001]

## Risk Management

Risk management. [REGIME_001]

## Final House View

Final view. [REGIME_001]

## Evidence Appendix

- [REGIME_001] market_regime_v2: risk_on.
"""


def test_knowledge_file_exists():
    assert framework_path().exists()
    assert "Act as a CIO" in load_cio_framework()


def test_snapshot_builder_includes_market_regime_v2():
    snapshot = build_cio_snapshot_from_data(sample_data())

    assert snapshot["market_regime_v2"]["market_regime"] == "risk_on"


def test_snapshot_builder_includes_sector_rotation():
    snapshot = build_cio_snapshot_from_data(sample_data())

    assert snapshot["sector_rotation"]["top_overweight"][0]["sector_name"] == "Semiconductors"


def test_snapshot_builder_includes_secular_themes():
    snapshot = build_cio_snapshot_from_data(sample_data())

    assert snapshot["secular_themes"]["top_themes"][0]["theme_name"] == "AI Infrastructure"


def test_snapshot_builder_includes_ticker_reference():
    snapshot = build_cio_snapshot_from_data(sample_data())

    assert snapshot["ticker_reference"]["CIBR"] == "Cybersecurity ETF"
    assert snapshot["approved_etf_label_map"]["XLE"] == "Energy ETF"


def test_snapshot_builder_includes_evidence_appendix():
    snapshot = build_cio_snapshot_from_data(sample_data())

    ids = {row["evidence_id"] for row in snapshot["evidence_appendix"]}
    assert "REGIME_001" in ids
    assert "SECTOR_ROTATION_001" in ids


def test_prompt_contains_framework_and_snapshot():
    framework = "Framework text"
    snapshot = build_cio_snapshot_from_data(sample_data())
    messages = build_cio_prompt(framework, snapshot)
    combined = "\n".join(message["content"] for message in messages)

    assert "Framework text" in combined
    assert "DATA SNAPSHOT" in combined
    assert "market_regime_v2" in combined
    assert "Analyze the data step by step internally" in combined


def test_output_validator_accepts_valid_markdown():
    assert validate_cio_markdown(VALID_MARKDOWN).startswith("# DeepSeek CIO House View")


def test_output_validator_strips_deepseek_thinking_text():
    markdown = "<think>private reasoning</think>\n" + VALID_MARKDOWN

    assert validate_cio_markdown(markdown).startswith("# DeepSeek CIO House View")


def test_output_validator_rejects_missing_headings():
    with pytest.raises(ValueError):
        validate_cio_markdown("# DeepSeek CIO House View\n\n## Executive Summary\n")


def test_output_validator_rejects_mislabeled_etf():
    bad = VALID_MARKDOWN.replace("Final view.", "Buy Grid Infrastructure ETF (SMH).")

    with pytest.raises(ValueError, match="mislabeled SMH"):
        validate_cio_markdown(bad)


def test_output_validator_allows_harmless_sector_etf_alias():
    good = VALID_MARKDOWN.replace("Final view.", "Energy Sector ETF (XLE) is underweight. [SECTOR_ROTATION_006]")

    assert validate_cio_markdown(good).startswith("# DeepSeek CIO House View")


def test_output_validator_rejects_unapproved_etf_label_alias():
    bad = VALID_MARKDOWN.replace("Final view.", "Oil ETF (XLE) remains underweight. [SECTOR_ROTATION_006]")

    with pytest.raises(ValueError, match="mislabeled XLE"):
        validate_cio_markdown(bad)


def test_output_validator_allows_number_inside_approved_label():
    good = VALID_MARKDOWN.replace("Final view.", "S&P 500 ETF (SPY) remains a core beta reference. [WATCHLIST_008]")

    assert validate_cio_markdown(good).startswith("# DeepSeek CIO House View")


def test_output_validator_rejects_unsupported_etf():
    bad = VALID_MARKDOWN.replace("Final view.", "Energy ETF (XOP) remains unsupported. [SECTOR_ROTATION_006]")

    with pytest.raises(ValueError, match="unsupported ticker"):
        validate_cio_markdown(bad)


def test_output_validator_rejects_wrong_vix_level():
    snapshot = build_cio_snapshot_from_data(sample_data())
    bad = VALID_MARKDOWN.replace("Final view.", "The VIX at 12.5 supports risk appetite.")

    with pytest.raises(ValueError, match="quoted VIX"):
        validate_cio_markdown(bad, snapshot=snapshot)


def test_output_validator_rejects_unsupported_number():
    snapshot = build_cio_snapshot_from_data(sample_data())
    bad = VALID_MARKDOWN.replace("Final view.", "Unsupported level 12345.")

    with pytest.raises(ValueError, match="unsupported number"):
        validate_cio_markdown(bad, snapshot=snapshot)


def test_output_validator_rejects_unsupported_ticker():
    snapshot = build_cio_snapshot_from_data(sample_data())
    bad = VALID_MARKDOWN.replace("Final view.", "Unsupported ticker ABCD.")

    with pytest.raises(ValueError, match="unsupported ticker"):
        validate_cio_markdown(bad, snapshot=snapshot)


def test_output_validator_rejects_claim_without_evidence_id():
    snapshot = build_cio_snapshot_from_data(sample_data())
    bad = VALID_MARKDOWN.replace("Summary. [REGIME_001]", "Summary without citation.")

    with pytest.raises(ValueError, match="lacks evidence IDs"):
        validate_cio_markdown(bad, snapshot=snapshot)


def test_no_evidence_lock_bypasses_quality_checks():
    snapshot = build_cio_snapshot_from_data(sample_data())
    bad = VALID_MARKDOWN.replace("Summary. [REGIME_001]", "Summary without citation and 12345.")

    assert validate_cio_markdown(bad, snapshot=snapshot, evidence_locked=False).startswith("# DeepSeek CIO House View")


def test_fallback_report_is_generated_on_invalid_response():
    snapshot = build_cio_snapshot_from_data(sample_data())
    markdown = fallback_cio_report(reason="missing heading", snapshot=snapshot)

    assert "# DeepSeek CIO House View" in markdown
    assert "Validation failure: missing heading" in markdown
    assert "## Final House View" in markdown


def test_dry_run_does_not_call_ollama_unless_call_model(monkeypatch):
    class Engine:
        pass

    called = {"value": False}

    def fake_chat(*args, **kwargs):
        called["value"] = True
        return {"message": {"content": VALID_MARKDOWN}}

    monkeypatch.setattr(
        "db_builder.deepseek_cio_engine.collect_report_data",
        lambda engine, window_hours: sample_data(),
    )
    result = generate_cio_report(Engine(), window_hours=24, call_model=False, chat_fn=fake_chat)

    assert result["status"] == "preview"
    assert called["value"] is False


def test_call_model_uses_chat_function(monkeypatch):
    class Engine:
        pass

    called = {"value": False}

    def fake_chat(*args, **kwargs):
        called["value"] = True
        return {"message": {"content": VALID_MARKDOWN}}

    monkeypatch.setattr(
        "db_builder.deepseek_cio_engine.collect_report_data",
        lambda engine, window_hours: sample_data(),
    )
    result = generate_cio_report(Engine(), window_hours=24, call_model=True, chat_fn=fake_chat)

    assert result["status"] == "valid"
    assert called["value"] is True


def test_save_mode_writes_markdown_path(tmp_path):
    path = save_deepseek_cio_report(VALID_MARKDOWN, reports_dir=tmp_path)

    assert path.name.startswith("deepseek_house_view_")
    assert path.suffix == ".md"
    assert path.read_text(encoding="utf-8") == VALID_MARKDOWN
