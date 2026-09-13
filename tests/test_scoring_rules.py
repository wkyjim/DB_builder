from __future__ import annotations

from datetime import datetime, timezone

from db_builder.contradiction_audit import audit_report_scores
from db_builder.market_strength import compute_market_strength, score_above_ma
from db_builder.news_scoring import classify_news_relevance, score_headline
from db_builder.report_renderer import render_rule_based_market_update, score_all
from db_builder.rule_based_config import MACRO_SYMBOLS
from db_builder.rule_based_regime import compute_regime, regime_label
from db_builder.sector_strength import rank_sectors
from db_builder.theme_strength import rank_themes, setup_label


def technical_rows():
    base = {
        "date": "2026-06-30",
        "close": 110,
        "ma_20": 105,
        "ma_50": 100,
        "ma_100": 95,
        "ma_200": 90,
        "rsi_14": 58,
        "macd": 2,
        "macd_signal": 1,
        "macd_hist": 1,
        "volume_ratio_20": 1.3,
        "return_5d": 2,
        "return_20d": 6,
        "return_60d": 12,
        "volatility_20d": 14,
    }
    rows = []
    for ticker in ["SPY", "QQQ", "IWM", "SMH", "SOXX", "CIBR", "XLV", "XLF", "XLE", "XAR", "GRID", "NLR", "XLU", "XLY", "XLK"]:
        rows.append({**base, "ticker": ticker})
    rows.append({**base, "ticker": "XLP", "close": 88, "ma_20": 90, "ma_50": 92, "ma_100": 95, "ma_200": 98, "return_20d": -4})
    return rows


def macro_rows():
    return [
        {"symbol": "^GSPC", "name": "S&P 500", "close": 6100, "pct_chg": 1.0, "date": "2026-06-30"},
        {"symbol": "^IXIC", "name": "Nasdaq", "close": 20000, "pct_chg": 1.5, "date": "2026-06-30"},
        {"symbol": "^RUT", "name": "Russell 2000", "close": 2200, "pct_chg": 0.7, "date": "2026-06-30"},
        {"symbol": "^VIX", "name": "VIX", "close": 15, "pct_chg": -4.0, "date": "2026-06-30"},
        {"symbol": "^FVX", "name": "5Y", "close": 4.0, "pct_chg": -0.5, "date": "2026-06-30"},
        {"symbol": "^TNX", "name": "10Y", "close": 4.3, "pct_chg": -0.4, "date": "2026-06-30"},
        {"symbol": "^TYX", "name": "30Y", "close": 4.8, "pct_chg": -0.2, "date": "2026-06-30"},
        {"symbol": "GC=F", "name": "Gold", "close": 2400, "pct_chg": -0.3, "date": "2026-06-30"},
        {"symbol": "SI=F", "name": "Silver", "close": 30, "pct_chg": 1.2, "date": "2026-06-30"},
        {"symbol": "CL=F", "name": "Oil", "close": 70, "pct_chg": 0.5, "date": "2026-06-30"},
        {"symbol": "HG=F", "name": "Copper", "close": 4.5, "pct_chg": 0.8, "date": "2026-06-30"},
    ]


def news_rows():
    return [
        {
            "title": "FOMC leaves rates stable as inflation expectations cool",
            "summary": "",
            "source_name": "Federal Reserve",
            "source_priority": 100,
            "published_at": datetime.now(timezone.utc),
            "sentiment_score": 0.3,
            "impact_score": 80,
            "confidence_score": 0.8,
            "themes": ["Fed", "Inflation"],
            "affected_tickers": ["SPY"],
        },
        {
            "title": "Should you buy these best stocks now",
            "summary": "",
            "source_name": "Generic",
            "source_priority": 30,
            "published_at": datetime.now(timezone.utc),
            "sentiment_score": 0.1,
            "impact_score": 50,
            "confidence_score": 0.5,
        },
    ]


def news_signals():
    return [
        {"dimension_value": "Semiconductors", "article_count": 4, "opportunity_score": 70, "risk_score": 20},
        {"dimension_value": "AI Infrastructure", "article_count": 5, "opportunity_score": 80, "risk_score": 25},
    ]


def economic_rows():
    return [
        {"series_id": "UNRATE", "series_name": "Unemployment Rate", "category": "labor", "date": "2026-05-01", "value": 4.3, "unit": "percent", "rn": 1},
        {"series_id": "UNRATE", "series_name": "Unemployment Rate", "category": "labor", "date": "2026-04-01", "value": 4.2, "unit": "percent", "rn": 2},
        {"series_id": "PAYEMS", "series_name": "All Employees, Total Nonfarm", "category": "labor", "date": "2026-05-01", "value": 159001, "unit": "thousands", "rn": 1},
        {"series_id": "PAYEMS", "series_name": "All Employees, Total Nonfarm", "category": "labor", "date": "2026-04-01", "value": 158800, "unit": "thousands", "rn": 2},
        {"series_id": "ICSA", "series_name": "Initial Claims", "category": "labor", "date": "2026-06-20", "value": 215000, "unit": "number", "rn": 1},
        {"series_id": "ICSA", "series_name": "Initial Claims", "category": "labor", "date": "2026-06-13", "value": 227000, "unit": "number", "rn": 2},
        {"series_id": "CPIAUCSL", "series_name": "Consumer Price Index", "category": "inflation", "date": "2026-05-01", "value": 333.9, "unit": "index", "rn": 1},
        {"series_id": "CPIAUCSL", "series_name": "Consumer Price Index", "category": "inflation", "date": "2026-04-01", "value": 333.1, "unit": "index", "rn": 2},
        {"series_id": "WB:CHN:NY.GDP.MKTP.KD.ZG", "series_name": "China GDP growth", "category": "growth", "date": "2025-01-01", "value": 4.96, "unit": "annual percent", "rn": 1},
        {"series_id": "ECB:EXR:D.USD.EUR.SP00.A", "series_name": "US dollar/Euro ECB reference exchange rate", "category": "fx", "date": "2026-06-30", "value": 1.1394, "unit": "USD", "rn": 1},
        {"series_id": "ECB:EXR:D.USD.EUR.SP00.A", "series_name": "US dollar/Euro ECB reference exchange rate", "category": "fx", "date": "2026-06-29", "value": 1.12, "unit": "USD", "rn": 2},
    ]


def sample_data():
    return {
        "generated_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
        "window_hours": 24,
        "technicals": technical_rows(),
        "macro": macro_rows(),
        "economic": economic_rows(),
        "news": news_rows(),
        "news_signals": news_signals(),
    }


def test_regime_label_thresholds():
    assert regime_label(82) == "Strong Risk-On"
    assert regime_label(50) == "Mixed / Rotation"
    assert regime_label(18) == "Defensive / Risk-Off"


def test_score_above_ma_strong_when_close_above_all_averages():
    assert score_above_ma(technical_rows()[0]) == 100


def test_market_strength_constructive_for_positive_tape():
    strength = compute_market_strength(technical_rows())

    assert strength["score"] > 60
    assert strength["label"] in {"constructive", "strong"}


def test_news_relevance_marks_generic_headline_noisy():
    assert classify_news_relevance(news_rows()[1]) == "noisy"
    assert score_headline(news_rows()[0])["news_score"] > score_headline(news_rows()[1])["news_score"]


def test_regime_computes_continuous_score():
    strength = compute_market_strength(technical_rows())
    regime = compute_regime(technical_rows(), macro_rows(), news_rows(), strength)

    assert 0 <= regime["score"] <= 100
    assert "subscores" in regime
    assert regime["label"] in {"Mild Risk-On", "Moderate Risk-On", "Strong Risk-On", "Mixed / Rotation"}


def test_sector_ranking_outputs_scores_and_labels():
    sectors = rank_sectors(technical_rows(), news_signals())

    assert sectors[0]["score"] >= sectors[-1]["score"]
    assert sectors[0]["trend_label"]


def test_theme_ranking_and_setup_labels():
    themes = rank_themes(technical_rows(), news_signals())

    assert themes[0]["setup_label"] in {
        "Strong outperformance setup",
        "Positive setup",
        "Neutral / watchlist",
        "Weak setup",
        "Underperformance risk",
    }
    assert setup_label(80) == "Strong outperformance setup"


def test_audit_flags_risk_on_with_weak_breadth():
    flags = audit_report_scores(
        {
            "regime": {"score": 70},
            "market_strength": {"breadth": {"score": 30}},
            "confidence": {"score": 80},
            "sectors": [],
            "themes": [],
            "news": {"score": 50},
        }
    )

    assert flags
    assert flags[0]["severity"] == "high"


def test_rule_based_report_renders_required_sections():
    scores = score_all(sample_data())
    markdown = render_rule_based_market_update(sample_data(), scores)

    for section in [
        "## Executive Dashboard",
        "## Market Regime Score",
        "## US Equity Strength Score",
        "## Evidence Quality / Confidence",
        "## Cross-Asset Confirmation",
        "## Market Dispersion Analysis",
        "## Sector Constituent Dispersion",
        "## Economic Data Snapshot",
        "## Sector and Theme Leadership",
        "### Official Sector Strength",
        "### Thematic Strength",
        "### Sector / Theme Alignment",
        "## Three-Month Outperformance Setup",
        "## News Analytics",
        "## Positioning & Flow Dashboard",
        "## Contradiction / Audit Flags",
        "## Data Quality Notes",
    ]:
        assert section in markdown
    assert "## Tactical Buy List" not in markdown
    assert "## Tactical Sell / Reduce List" not in markdown
    assert "overweight" not in markdown.lower()


def test_macro_symbols_include_dashboard_tape_assets():
    expected = {
        "^GSPC",
        "^NDX",
        "^DJI",
        "^VIX",
        "^HSI",
        "NIY=F",
        "^KS200",
        "DX-Y.NYB",
        "JPY=X",
        "EURUSD=X",
        "GC=F",
        "BZ=F",
        "CL=F",
        "BTC-USD",
    }

    assert expected <= set(MACRO_SYMBOLS)


def test_rule_based_report_includes_precious_and_cyclical_metals_analysis():
    markdown = render_rule_based_market_update(sample_data(), score_all(sample_data()))

    assert "| Gold |" in markdown
    assert "| Silver |" in markdown
    assert "| Copper |" in markdown
    assert "SI=F" in markdown


def test_rule_based_report_labels_live_macro_rows():
    data = sample_data()
    data["macro"] = [
        {
            "symbol": "^GSPC",
            "name": "S&P 500",
            "close": 6200,
            "pct_chg": 0.8,
            "date": "2026-07-21",
            "market_date": "2026-07-21",
            "observed_at": "2026-07-21T14:05:00+00:00",
            "is_live": True,
            "data_status": "live",
        },
        *macro_rows()[1:],
    ]

    markdown = render_rule_based_market_update(data, score_all(data))

    assert "Live macro rows are intraday snapshots" in markdown
    assert "| Symbol | Name | Close | Pct Chg | Market Date | Status |" in markdown
    assert "live as of 21 July 2026, 22:05:00 (HKT)" in markdown
    assert "- Live macro rows used: `1`" in markdown


def test_rule_based_report_uses_readable_hkt_generated_timestamp_and_regime_table():
    markdown = render_rule_based_market_update(sample_data(), score_all(sample_data()))

    assert "Generated at: 30 June 2026, 08:00:00 (HKT)" in markdown
    assert "| Metric | Value | Driver / Interpretation |" in markdown
    assert "| Overall regime |" in markdown
    assert "Positive contributors" in markdown
    assert "Negative contributors" in markdown
    assert "Core equity ETFs versus moving averages." in markdown
    assert "higher score means calmer volatility conditions" in markdown


def test_news_analytics_table_uses_investment_implication_format():
    markdown = render_rule_based_market_update(sample_data(), score_all(sample_data()))

    assert "### Top Market-Moving Headlines" in markdown
    assert "Investment implication" in markdown
    assert "Affected assets" in markdown
    assert "short-term" in markdown


def test_rule_based_report_includes_latest_economic_data_analysis():
    markdown = render_rule_based_market_update(sample_data(), score_all(sample_data()))

    assert "### U.S. Labor" in markdown
    assert "Unemployment Rate" in markdown
    assert "Higher reading increases macro pressure." in markdown
    assert "Lower claims indicate firmer labor-market conditions." in markdown
    assert "### Global Structural Snapshot" in markdown
    assert "China GDP growth" in markdown
    assert "### ECB FX Snapshot" in markdown


def test_rule_based_report_renders_positioning_flow_rows():
    data = sample_data()
    data["positioning_flow"] = [
        {
            "signal_date": "2026-06-30",
            "asset_id": "SPX",
            "signal_name": "COT net position pct open interest",
            "signal_value": 0.25,
            "z_score": 2.1,
            "percentile": 0.97,
            "interpretation": "Crowded long positioning.",
            "source": "CFTC COT",
        },
        {
            "signal_date": "2026-07-02",
            "asset_id": "NVDA",
            "signal_name": "FINRA short-sale volume ratio",
            "signal_value": 0.55,
            "z_score": 2.4,
            "interpretation": "Elevated short-sale volume.",
            "source": "FINRA short-sale volume",
        },
        {
            "signal_date": "2026-07-10",
            "asset_id": "QQQ",
            "asset_group": "Growth / Nasdaq",
            "display_name": "QQQ - Growth / Nasdaq",
            "flow_bucket": "Broad Market ETF Flows",
            "signal_name": "ETF daily net fund flow",
            "signal_value": 250000000,
            "z_score": 400000000,
            "flow_comment": "1D inflow; 5D inflow.",
            "source": "ETF daily data",
        },
        {
            "signal_date": "2026-07-10",
            "asset_id": "XLK",
            "asset_group": "Technology",
            "display_name": "XLK - Technology",
            "flow_bucket": "Sector / Thematic ETF Flows",
            "signal_name": "ETF daily net fund flow",
            "signal_value": -1250000,
            "z_score": 2500000,
            "flow_comment": "1D outflow; 5D inflow.",
            "source": "ETF daily data",
        },
    ]

    markdown = render_rule_based_market_update(data)

    assert "### Futures Positioning" in markdown
    assert "### ETF Fund Flows" not in markdown
    assert "**Broad Market ETF Flows**" not in markdown
    assert "**Sector / Thematic ETF Flows**" not in markdown
    assert "QQQ - Growth / Nasdaq" not in markdown
    assert "XLK - Technology" not in markdown
    assert "### Short-Sale Pressure" not in markdown
    assert "FINRA short-sale volume is not short interest" not in markdown


def test_rule_based_report_excludes_short_positioning():
    data = sample_data()
    data["short_analytics"] = [
        {
            "analytics_date": "2026-08-11",
            "ticker": "AAA",
            "short_regime": "STRUCTURAL_FUNDING_SHORT",
            "regime_confidence": 82,
            "funding_short_score": 80,
            "short_position_score": 78,
            "short_activity_score": 55,
            "unwind_risk_score": 20,
            "days_to_cover": 2,
            "rel_return_3m": -0.1,
        }
    ]

    markdown = render_rule_based_market_update(data)

    assert "## Short Positioning Analytics" not in markdown
    assert "Structural Funding Short" not in markdown
    assert "not proof of hedge-fund identity" not in markdown
