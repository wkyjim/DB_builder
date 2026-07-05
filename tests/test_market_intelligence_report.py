from __future__ import annotations

from datetime import datetime, timezone

from db_builder.market_intelligence_report import (
    render_daily_house_view_report,
    render_market_pulse_report,
    top_news_articles,
)


def sample_data():
    return {
        "generated_at": datetime(2026, 6, 9, 1, 0, tzinfo=timezone.utc),
        "window_hours": 24,
        "regime": [{"regime_label": "risk_on", "confidence_score": 0.17}],
        "regime_v2": [
            {
                "market_regime": "neutral",
                "market_phase": "range_bound",
                "confidence": 0.0949,
                "technical_score": 48,
                "momentum_score": 54,
                "breadth_score": 58,
                "risk_appetite_score": 42,
                "news_score": 51,
                "market_strength": "weak",
                "trend_state": "neutral",
                "momentum_state": "stable_positive",
                "volatility_state": "stressed",
                "breadth_state": "healthy",
                "risk_appetite_state": "neutral",
                "drivers": {"risk": ["VIX spike"]},
            }
        ],
        "sector_regimes": [
            {"sector_name": "Cybersecurity", "sector_regime": "bull", "cycle_phase": "early_bull", "related_etfs": ["CIBR"]},
            {"sector_name": "Semiconductors", "sector_regime": "bull", "cycle_phase": "early_bull", "related_etfs": ["SMH", "SOXX"]},
            {"sector_name": "Healthcare", "sector_regime": "bull", "cycle_phase": "early_bull", "related_etfs": ["XLV"]},
            {"sector_name": "Energy", "sector_regime": "strong_bear", "cycle_phase": "bear", "related_etfs": ["XLE"]},
            {"sector_name": "Nuclear", "sector_regime": "strong_bear", "cycle_phase": "bear", "related_etfs": ["NLR"]},
        ],
        "sector_rotation": [
            {"sector_name": "Cybersecurity", "rotation_rank": 1, "rotation_score": 61, "allocation_bias": "overweight", "related_etfs": ["CIBR"]},
            {"sector_name": "Semiconductors", "rotation_rank": 2, "rotation_score": 58, "allocation_bias": "overweight", "related_etfs": ["SMH", "SOXX"]},
            {"sector_name": "Healthcare", "rotation_rank": 3, "rotation_score": 54, "allocation_bias": "overweight", "related_etfs": ["XLV"]},
            {"sector_name": "Energy", "rotation_rank": 12, "rotation_score": 22, "allocation_bias": "avoid", "related_etfs": ["XLE"]},
            {"sector_name": "Nuclear", "rotation_rank": 13, "rotation_score": 20, "allocation_bias": "avoid", "related_etfs": ["NLR"]},
        ],
        "secular_themes": [
            {"theme_name": "Cybersecurity", "parent_theme": "Security", "secular_score": 85, "tactical_score": 70, "related_etfs": ["CIBR"]},
            {"theme_name": "AI Infrastructure", "parent_theme": "AI & Compute", "secular_score": 88, "tactical_score": 45, "related_etfs": ["QQQ", "SMH"]},
            {"theme_name": "Nuclear", "parent_theme": "Energy", "secular_score": 75, "tactical_score": 44, "related_etfs": ["NLR"]},
            {"theme_name": "Energy Security", "parent_theme": "Energy", "secular_score": 73, "tactical_score": 42, "related_etfs": ["XLE"]},
        ],
        "news_signals": [
            {"dimension_type": "theme", "dimension_value": "Geopolitics", "risk_score": 61, "opportunity_score": 12},
            {"dimension_type": "theme", "dimension_value": "AI", "risk_score": 54, "opportunity_score": 84},
        ],
        "opportunities": [],
        "macro": [
            {"symbol": "^GSPC", "name": "S&P 500", "date": "2026-06-09", "close": 6100, "pct_chg": 0.2},
            {"symbol": "^IXIC", "name": "Nasdaq", "date": "2026-06-09", "close": 20000, "pct_chg": -0.4},
            {"symbol": "^RUT", "name": "Russell 2000", "date": "2026-06-09", "close": 2200, "pct_chg": 0.1},
            {"symbol": "^FVX", "name": "5Y Treasury", "date": "2026-06-09", "close": 4.1, "pct_chg": -1.0},
            {"symbol": "^TNX", "name": "10Y Treasury", "date": "2026-06-09", "close": 4.3, "pct_chg": -0.8},
            {"symbol": "^TYX", "name": "30Y Treasury", "date": "2026-06-09", "close": 4.8, "pct_chg": -0.5},
            {"symbol": "GC=F", "name": "Gold", "date": "2026-06-09", "close": 2400, "pct_chg": 0.7},
            {"symbol": "CL=F", "name": "Oil", "date": "2026-06-09", "close": 72, "pct_chg": -1.2},
            {"symbol": "HG=F", "name": "Copper", "date": "2026-06-09", "close": 4.6, "pct_chg": 0.5},
            {"symbol": "^VIX", "name": "VIX", "date": "2026-06-09", "close": 18, "pct_chg": 39},
            {"symbol": "BTC-USD", "name": "Bitcoin", "date": "2026-06-09", "close": 100000, "pct_chg": 2},
        ],
        "watchlist": [
            {"ticker": "SPY", "close": 610, "pct_chg": 0.2, "rsi_14": 55},
            {"ticker": "QQQ", "close": 520, "pct_chg": -4.8, "rsi_14": 42},
            {"ticker": "SMH", "close": 250, "pct_chg": -9.2, "rsi_14": 35},
        ],
        "articles": [
            {
                "title": "Stocks rise in market today",
                "summary": "",
                "source_name": "Yahoo Finance",
                "source_category": "markets",
                "source_priority": 50,
                "impact_score": 80,
                "published_at": "2026-06-09T01:00:00Z",
                "themes": ["Market Sentiment"],
            },
            {
                "title": "Fed enforcement action hits bank liquidity",
                "summary": "",
                "source_name": "Federal Reserve",
                "source_category": "policy",
                "source_priority": 100,
                "impact_score": 60,
                "published_at": "2026-06-08T23:00:00Z",
                "themes": ["Fed", "Financials"],
            },
        ],
    }


def test_daily_report_does_not_duplicate_old_market_regime_by_default():
    markdown = render_daily_house_view_report(sample_data())

    assert "## Market Regime\n" not in markdown
    assert "Old Market Regime Model" not in markdown


def test_daily_report_includes_all_house_view_sections():
    markdown = render_daily_house_view_report(sample_data())

    for heading in [
        "## Executive Summary",
        "## Market Snapshot",
        "## Market Bullishness / Bearishness",
        "## Investor Sentiment",
        "## Sector Strength Ranking",
        "## Highest Conviction Opportunities",
        "## Tactical Positioning",
        "## Tactical Buy List",
        "## Tactical Sell / Reduce List",
        "## Key Risks",
        "## Risk Management",
        "## Final House View",
    ]:
        assert heading in markdown


def test_daily_report_market_snapshot_uses_local_macro_rows():
    markdown = render_daily_house_view_report(sample_data())

    assert "## Market Snapshot" in markdown
    assert "- S&P 500: **stable**; close `6100.0`, pct_chg `0.2%`, date `2026-06-09`" in markdown
    assert "- Nasdaq: **down**; close `20000.0`, pct_chg `-0.4%`, date `2026-06-09`" in markdown
    assert "- Yield Curve: upward sloping; 5Y `4.1`, 10Y `4.3`, 30Y `4.8`" in markdown
    assert "- VIX: **up**; close `18.0`, pct_chg `39.0%`, date `2026-06-09`" in markdown
    assert "- U.S. Dollar / DXY: unavailable from local PostgreSQL snapshot." in markdown


def test_pulse_report_is_shorter_and_includes_top_headlines():
    markdown = render_market_pulse_report(sample_data())

    assert "## Market Pulse Summary" in markdown
    assert "## Top News Intelligence" in markdown
    assert len(markdown.split()) < 900


def test_appendix_flag_includes_raw_diagnostics():
    markdown = render_daily_house_view_report(sample_data(), include_appendix=True)

    assert "## Appendix / Raw Diagnostics" in markdown
    assert "Old Market Regime Model" in markdown


def test_no_appendix_by_default():
    markdown = render_daily_house_view_report(sample_data())

    assert "## Appendix / Raw Diagnostics" not in markdown


def test_zero_opportunities_produces_thematic_wording():
    markdown = render_daily_house_view_report(sample_data())

    assert "Thematic opportunities remain available" in markdown
    assert "Tier 1" in markdown


def test_sector_ranking_consolidates_regime_rotation_and_themes():
    markdown = render_daily_house_view_report(sample_data())

    assert "| 1 | Cybersecurity | Overweight | CIBR | bull | High |" in markdown


def test_top_news_intelligence_ranks_premium_important_headlines_first():
    articles = top_news_articles(sample_data(), limit=2)

    assert articles[0]["source_name"] == "Federal Reserve"


def test_final_house_view_uses_critical_pm_view():
    markdown = render_daily_house_view_report(sample_data())

    assert "neutral/range-bound with selective risk appetite" in markdown
