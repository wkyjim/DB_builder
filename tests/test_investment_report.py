from __future__ import annotations

from datetime import datetime, timezone

from db_builder.investment_report import (
    append_quality_summary,
    normalize_quality_summary,
    render_investment_report,
    render_quality_summary,
    save_report,
)


GENERATED_AT = datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)


def sample_data(opportunities=None, article_count=2):
    articles = [
        {
            "article_id": "00000000-0000-0000-0000-000000000001",
            "title": "AI infrastructure spending accelerates",
            "source_name": "Example News",
            "url": "https://example.com/ai",
        }
    ]
    if article_count == 0:
        articles = []

    return {
        "generated_at": GENERATED_AT,
        "window_hours": 24,
        "regime": [
            {
                "regime_label": "risk_off",
                "confidence_score": 0.42,
                "risk_on_score": 100,
                "risk_off_score": 180,
                "news_signal_count": 3,
                "macro_signal_count": 5,
                "drivers": ["macro:^VIX pct_chg=4.0"],
            }
        ],
        "regime_v2": [
            {
                "market_regime": "risk_off",
                "market_phase": "correction_in_bull",
                "confidence": 0.38,
                "market_strength": "moderate",
                "trend_state": "downtrend",
                "momentum_state": "negative",
                "volatility_state": "elevated",
                "breadth_state": "weak",
                "risk_appetite_state": "risk_reducing",
                "drivers": {"risk_appetite": ["VIX close=24 pct_chg=6"], "technical": ["QQQ technical=35"]},
            }
        ],
        "news_signals": [
            {
                "dimension_type": "theme",
                "dimension_value": "AI",
                "article_count": 1,
                "weighted_sentiment_score": 0.5,
                "opportunity_score": 42,
                "risk_score": 12,
                "top_article_ids": ["00000000-0000-0000-0000-000000000001"],
            },
            {
                "dimension_type": "theme",
                "dimension_value": "Rates",
                "article_count": 1,
                "weighted_sentiment_score": -0.4,
                "opportunity_score": 5,
                "risk_score": 55,
                "top_article_ids": [],
            },
        ],
        "opportunities": opportunities or [],
        "sector_signals": [
            {
                "sector_name": "Semiconductors",
                "related_etfs": ["SMH", "SOXX"],
                "opportunity_score": 81,
                "risk_score": 22,
                "momentum_score": 72,
                "trend_score": 70,
                "final_score": 65.5,
                "top_themes": ["AI", "Semiconductors"],
            }
        ],
        "sector_regimes": [
            {
                "sector_name": "Semiconductors",
                "related_etfs": ["SMH", "SOXX"],
                "sector_regime": "bull",
                "cycle_phase": "mid_bull",
                "confidence": 0.44,
                "final_score": 72,
            }
        ],
        "sector_rotation": [
            {
                "sector_name": "Semiconductors",
                "related_etfs": ["SMH", "SOXX"],
                "rotation_rank": 1,
                "rotation_score": 70,
                "allocation_bias": "overweight",
                "recommended_action": "add",
            },
            {
                "sector_name": "Real Estate",
                "related_etfs": ["XLRE"],
                "rotation_rank": 12,
                "rotation_score": 32,
                "allocation_bias": "underweight",
                "recommended_action": "trim",
            },
        ],
        "articles": articles,
        "macro": [{"symbol": "^VIX", "name": "Volatility", "pct_chg": 4.0, "close": 20}],
        "watchlist": [{"ticker": "SMH", "close": 250, "pct_chg": 1.2, "rsi_14": 55.2}],
    }


def test_report_includes_required_sections_and_regime_confidence():
    markdown = render_investment_report(sample_data())

    assert "## Executive Summary" in markdown
    assert "## Market Regime" in markdown
    assert "## Market Regime 2.0" in markdown
    assert "## Top News Themes" in markdown
    assert "## Risk Signals" in markdown
    assert "## Opportunity Signals" in markdown
    assert "## Sector Intelligence" in markdown
    assert "## Sector Regimes" in markdown
    assert "## Sector Rotation" in markdown
    assert "## Watchlist Commentary" in markdown
    assert "## Data Quality Notes" in markdown
    assert "Confidence: `0.42`" in markdown


def test_report_explicitly_states_when_no_opportunities_pass_filters():
    markdown = render_investment_report(sample_data(opportunities=[]))

    assert "No high-conviction opportunities passed current filters." in markdown


def test_report_includes_top_articles_behind_themes():
    markdown = render_investment_report(sample_data())

    assert "[AI infrastructure spending accelerates](https://example.com/ai) (Example News)" in markdown


def test_report_warns_when_article_count_is_low():
    markdown = render_investment_report(sample_data(article_count=0))

    assert "Warning: article count is low" in markdown


def test_report_renders_opportunity_signals_when_available():
    markdown = render_investment_report(
        sample_data(
            opportunities=[
                {
                    "ticker": "SMH",
                    "signal_label": "opportunity",
                    "opportunity_score": 45,
                    "risk_score": 10,
                    "technical_score": 12,
                    "reasons": ["news opportunity=33 risk=10"],
                }
            ]
        )
    )

    assert "**SMH** (opportunity)" in markdown
    assert "news opportunity=33 risk=10" in markdown


def test_report_renders_sector_intelligence():
    markdown = render_investment_report(sample_data())

    assert "**Semiconductors**: final `65.5`" in markdown
    assert "ETFs `SMH, SOXX`" in markdown
    assert "Top themes: AI, Semiconductors" in markdown


def test_report_renders_market_regime_v2():
    markdown = render_investment_report(sample_data())

    assert "Market regime: **risk_off**" in markdown
    assert "Market phase: **correction_in_bull**" in markdown
    assert "risk_appetite: VIX close=24 pct_chg=6" in markdown


def test_report_renders_sector_regimes_and_rotation():
    markdown = render_investment_report(sample_data())

    assert "**Semiconductors**: regime `bull`, phase `mid_bull`" in markdown
    assert "Overweight candidates:" in markdown
    assert "**Semiconductors**: rank `1`, score `70`" in markdown
    assert "Underweight / avoid candidates:" in markdown
    assert "**Real Estate**: bias `underweight`" in markdown


def test_report_sector_intelligence_fallback():
    data = sample_data()
    data["sector_signals"] = []
    markdown = render_investment_report(data)

    assert "No sector intelligence signals are available yet." in markdown


def test_save_report_uses_timestamped_markdown_name(tmp_path):
    path = save_report("hello\n", generated_at=GENERATED_AT, reports_dir=tmp_path)

    assert path.name == "investment_report_20260607_123000.md"
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_normalize_quality_summary_enforces_five_summary_bullets():
    summary = normalize_quality_summary(
        {
            "executive_summary": ["One", "Two"],
            "top_risks": ["Risk"],
            "top_opportunities": [],
            "positioning_bias": "Defensive.",
        }
    )

    assert len(summary["executive_summary"]) == 5
    assert summary["executive_summary"][0] == "One"
    assert summary["top_risks"] == ["Risk"]
    assert summary["positioning_bias"] == "Defensive."


def test_render_quality_summary_includes_required_sections():
    markdown = render_quality_summary(
        {
            "executive_summary": ["A", "B", "C", "D", "E"],
            "top_risks": ["Risk one"],
            "top_opportunities": ["Opportunity one"],
            "positioning_bias": "Defensive with dry powder.",
        }
    )

    assert "## Report Quality Summary" in markdown
    assert "### 5-Bullet Executive Summary" in markdown
    assert "### Top Risks" in markdown
    assert "### Top Opportunities" in markdown
    assert "### Positioning Bias" in markdown


def test_append_quality_summary_uses_injected_summarizer():
    def fake_summarizer(markdown, *, timeout):
        assert "base report" in markdown
        assert timeout == 3
        return {
            "executive_summary": ["A", "B", "C", "D", "E"],
            "top_risks": ["Risk one"],
            "top_opportunities": ["No high-conviction opportunities."],
            "positioning_bias": "Risk-off.",
        }

    markdown = append_quality_summary("base report\n", timeout=3, summarizer=fake_summarizer)

    assert "## Report Quality Summary" in markdown
    assert "- Risk-off." in markdown


def test_append_quality_summary_falls_back_on_model_error():
    def failing_summarizer(markdown, *, timeout):
        raise RuntimeError("ollama unavailable")

    markdown = append_quality_summary("base report\n", summarizer=failing_summarizer)

    assert "Quality summary could not be generated" in markdown
    assert "ollama unavailable" in markdown
