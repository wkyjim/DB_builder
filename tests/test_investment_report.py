from __future__ import annotations

from datetime import datetime, timezone

from db_builder.investment_report import render_investment_report, save_report


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
        "articles": articles,
        "macro": [{"symbol": "^VIX", "name": "Volatility", "pct_chg": 4.0, "close": 20}],
        "watchlist": [{"ticker": "SMH", "close": 250, "pct_chg": 1.2, "rsi_14": 55.2}],
    }


def test_report_includes_required_sections_and_regime_confidence():
    markdown = render_investment_report(sample_data())

    assert "## Executive Summary" in markdown
    assert "## Market Regime" in markdown
    assert "## Top News Themes" in markdown
    assert "## Risk Signals" in markdown
    assert "## Opportunity Signals" in markdown
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


def test_save_report_uses_timestamped_markdown_name(tmp_path):
    path = save_report("hello\n", generated_at=GENERATED_AT, reports_dir=tmp_path)

    assert path.name == "investment_report_20260607_123000.md"
    assert path.read_text(encoding="utf-8") == "hello\n"
