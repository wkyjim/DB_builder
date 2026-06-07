from __future__ import annotations

from datetime import datetime, timezone

from db_builder.news_signal_aggregation import (
    aggregate_signal_records,
    article_quality_score,
    expand_signal_dimensions,
    source_weight_factor,
)
from db_builder.news_taxonomy import canonical_theme, normalize_theme_key, normalize_ticker


RUN_TIME = datetime(2026, 6, 6, 12, tzinfo=timezone.utc)


def fake_records():
    return [
        {
            "article_id": "00000000-0000-0000-0000-000000000001",
            "sentiment_score": 0.8,
            "impact_score": 80,
            "confidence_score": 0.9,
            "themes": ["AI", "semiconductors"],
            "affected_tickers": ["NVDA"],
            "raw_response": {"asset_classes": ["equity"], "regions": ["US"]},
            "source_priority": 100,
        },
        {
            "article_id": "00000000-0000-0000-0000-000000000002",
            "sentiment_score": -0.4,
            "impact_score": 70,
            "confidence_score": 0.8,
            "themes": ["AI"],
            "affected_tickers": ["MSFT"],
            "raw_response": {},
            "source_priority": 50,
        },
        {
            "article_id": "00000000-0000-0000-0000-000000000003",
            "sentiment_score": 0.0,
            "impact_score": 20,
            "confidence_score": 0.5,
            "themes": [],
            "affected_tickers": [],
            "raw_response": {},
        },
    ]


def test_expand_signal_dimensions_skips_empty_and_includes_optional_fields():
    dimensions = expand_signal_dimensions(fake_records()[0])

    assert ("theme", "AI") in dimensions
    assert ("ticker", "NVDA") in dimensions
    assert ("asset_class", "equity") in dimensions
    assert ("region", "US") in dimensions
    assert expand_signal_dimensions(fake_records()[2]) == []


def test_aggregate_theme_signal_counts_and_weighted_sentiment():
    signals = aggregate_signal_records(fake_records(), window_hours=24, run_time=RUN_TIME)
    ai_signal = next(s for s in signals if s["dimension_type"] == "theme" and s["dimension_value"] == "AI")

    expected = ((0.8 * 80 * 0.9 * 1.0) + (-0.4 * 70 * 0.8 * 0.5)) / (
        (80 * 0.9 * 1.0) + (70 * 0.8 * 0.5)
    )

    assert ai_signal["article_count"] == 2
    assert ai_signal["high_impact_count"] == 2
    assert ai_signal["positive_count"] == 1
    assert ai_signal["negative_count"] == 1
    assert ai_signal["weighted_sentiment_score"] == round(expected, 6)
    assert ai_signal["top_article_ids"][0] == "00000000-0000-0000-0000-000000000001"


def test_opportunity_and_risk_scores_reflect_sentiment_direction():
    signals = aggregate_signal_records(fake_records(), window_hours=24, run_time=RUN_TIME)
    nvda = next(s for s in signals if s["dimension_type"] == "ticker" and s["dimension_value"] == "NVDA")
    msft = next(s for s in signals if s["dimension_type"] == "ticker" and s["dimension_value"] == "MSFT")

    assert nvda["opportunity_score"] > nvda["risk_score"]
    assert msft["risk_score"] > msft["opportunity_score"]


def test_canonical_theme_mapping():
    assert canonical_theme("artificial intelligence") == "AI"
    assert canonical_theme("ai") == "AI"
    assert canonical_theme("stock market") == "Market Sentiment"
    assert canonical_theme("investment performance") == "Market Sentiment"
    assert canonical_theme("treasury yields") == "Rates"
    assert canonical_theme("federal reserve") == "Fed"
    assert canonical_theme("crude oil") == "Oil"
    assert canonical_theme("defense spending") == "Defense"
    assert canonical_theme("nuclear energy") == "Nuclear"
    assert canonical_theme("monetary policy") == "Monetary Policy"
    assert canonical_theme("bank capital") == "Bank Regulation"
    assert canonical_theme("crypto regulation") == "Crypto Regulation"
    assert canonical_theme("trade policy") == "Trade Policy"
    assert canonical_theme("treasury market") == "Treasury Market"
    assert canonical_theme("energy security") == "Energy Security"
    assert canonical_theme("national security") == "National Security"
    assert canonical_theme("equity markets") == "Market Sentiment"
    assert canonical_theme("stocks") == "Market Sentiment"
    assert canonical_theme("gaming sector") == "Consumer Discretionary"
    assert canonical_theme("technology sector") == "Technology"
    assert canonical_theme("banking sector") == "Financials"
    assert canonical_theme("cryptocurrency market") == "Crypto"
    assert canonical_theme("healthcare sector") == "Healthcare"
    assert canonical_theme("energy sector") == "Energy"
    assert canonical_theme("retail sector") == "Consumer Discretionary"
    assert canonical_theme("geo-political") == "Geopolitics"
    assert canonical_theme("geopolitical risk") == "Geopolitics"
    assert canonical_theme("geopolitical_risk") == "Geopolitics"
    assert canonical_theme("war risk") == "Geopolitics"
    assert canonical_theme("middle east risk") == "Geopolitics"
    assert canonical_theme("iran risk") == "Geopolitics"
    assert canonical_theme("oil_market") == "Oil"
    assert canonical_theme("oil market") == "Oil"
    assert canonical_theme("oil_prices") == "Oil"
    assert canonical_theme("oil prices") == "Oil"
    assert canonical_theme("crude oil prices") == "Oil"
    assert canonical_theme("brent crude") == "Oil"
    assert canonical_theme("wti crude") == "Oil"
    assert canonical_theme("inflation expectations") == "Inflation"
    assert canonical_theme("interest rate policy") == "Interest Rates"
    assert canonical_theme("fed policy") == "Monetary Policy"
    assert canonical_theme("central bank policy") == "Central Banks"
    assert canonical_theme("trade war") == "Trade Policy"
    assert canonical_theme("tariff risk") == "Trade Policy"
    assert canonical_theme("regulatory risk") == "Regulation"
    assert canonical_theme("sec enforcement") == "Regulation"


def test_theme_key_normalization_handles_separators_and_spaces():
    assert normalize_theme_key(" geopolitical_risk ") == "geopolitical risk"
    assert normalize_theme_key("geo-political") == "geo political"
    assert normalize_theme_key("oil   prices") == "oil prices"


def test_ticker_normalization_rejects_generic_regions_and_bad_dotted_values():
    assert normalize_ticker("nvda") == "NVDA"
    assert normalize_ticker("BRK.B") == "BRK.B"
    assert normalize_ticker("U.S.") is None
    assert normalize_ticker("US") is None
    assert normalize_ticker("USA") is None
    assert normalize_ticker("BAD.VALUE") is None


def test_aggregation_uses_canonical_themes():
    records = [
        {
            "article_id": "00000000-0000-0000-0000-000000000004",
            "sentiment_score": 0.5,
            "impact_score": 80,
            "confidence_score": 0.9,
            "themes": ["artificial intelligence", "AI", "stock market"],
            "affected_tickers": ["U.S.", "nvda"],
            "raw_response": {},
        }
    ]

    signals = aggregate_signal_records(records, window_hours=24, run_time=RUN_TIME)
    dimensions = {(signal["dimension_type"], signal["dimension_value"]) for signal in signals}

    assert ("theme", "AI") in dimensions
    assert ("theme", "Market Sentiment") in dimensions
    assert ("ticker", "NVDA") in dimensions
    assert ("ticker", "U.S.") not in dimensions


def test_duplicate_canonical_dimensions_are_counted_once_per_article():
    record = {
        "article_id": "00000000-0000-0000-0000-000000000005",
        "sentiment_score": 0.4,
        "impact_score": 60,
        "confidence_score": 0.8,
        "themes": ["artificial intelligence", "AI", "stock market", "investment performance"],
        "affected_tickers": ["nvda", "NVDA"],
        "raw_response": {},
    }

    dimensions = expand_signal_dimensions(record)
    signals = aggregate_signal_records([record], window_hours=24, run_time=RUN_TIME)
    ai_signal = next(s for s in signals if s["dimension_type"] == "theme" and s["dimension_value"] == "AI")
    market_signal = next(
        s for s in signals if s["dimension_type"] == "theme" and s["dimension_value"] == "Market Sentiment"
    )
    nvda_signal = next(s for s in signals if s["dimension_type"] == "ticker" and s["dimension_value"] == "NVDA")

    assert dimensions.count(("theme", "AI")) == 1
    assert dimensions.count(("theme", "Market Sentiment")) == 1
    assert dimensions.count(("ticker", "NVDA")) == 1
    assert ai_signal["article_count"] == 1
    assert market_signal["article_count"] == 1
    assert nvda_signal["article_count"] == 1


def test_source_weight_factor_defaults_and_clamps():
    assert source_weight_factor({"source_priority": 100}) == 1.0
    assert source_weight_factor({"source_priority": 40}) == 0.4
    assert source_weight_factor({"source_priority": 200}) == 1.0
    assert source_weight_factor({"source_priority": None}) == 0.75


def test_weighted_sentiment_uses_source_priority():
    records = [
        {
            "article_id": "00000000-0000-0000-0000-000000000006",
            "sentiment_score": 1.0,
            "impact_score": 100,
            "confidence_score": 1.0,
            "themes": ["Monetary Policy"],
            "affected_tickers": [],
            "raw_response": {},
            "source_priority": 100,
        },
        {
            "article_id": "00000000-0000-0000-0000-000000000007",
            "sentiment_score": -1.0,
            "impact_score": 100,
            "confidence_score": 1.0,
            "themes": ["Monetary Policy"],
            "affected_tickers": [],
            "raw_response": {},
            "source_priority": 40,
        },
    ]

    signals = aggregate_signal_records(records, window_hours=24, run_time=RUN_TIME)
    signal = next(s for s in signals if s["dimension_value"] == "Monetary Policy")

    expected = ((1.0 * 100 * 1.0 * 1.0) + (-1.0 * 100 * 1.0 * 0.4)) / ((100 * 1.0 * 1.0) + (100 * 1.0 * 0.4))

    assert signal["weighted_sentiment_score"] == round(expected, 6)
    assert signal["top_article_ids"][0] == "00000000-0000-0000-0000-000000000006"


def test_top_articles_prefer_premium_source_when_scores_are_similar():
    records = [
        {
            "article_id": "00000000-0000-0000-0000-000000000008",
            "sentiment_score": -0.3,
            "impact_score": 80,
            "confidence_score": 0.9,
            "themes": ["Geopolitics"],
            "affected_tickers": [],
            "raw_response": {},
            "source_name": "Yahoo Finance",
            "source_priority": 50,
            "published_at": datetime(2026, 6, 7, 12, tzinfo=timezone.utc),
        },
        {
            "article_id": "00000000-0000-0000-0000-000000000009",
            "sentiment_score": -0.3,
            "impact_score": 78,
            "confidence_score": 0.9,
            "themes": ["Geopolitics"],
            "affected_tickers": [],
            "raw_response": {},
            "source_name": "Federal Reserve",
            "source_priority": 100,
            "published_at": datetime(2026, 6, 7, 10, tzinfo=timezone.utc),
        },
    ]

    signals = aggregate_signal_records(records, window_hours=24, run_time=RUN_TIME)
    signal = next(s for s in signals if s["dimension_value"] == "Geopolitics")

    assert article_quality_score(records[1]) > article_quality_score(records[0])
    assert signal["top_article_ids"][0] == "00000000-0000-0000-0000-000000000009"


def test_top_articles_tie_break_by_published_at_desc():
    records = [
        {
            "article_id": "00000000-0000-0000-0000-000000000010",
            "sentiment_score": 0.2,
            "impact_score": 80,
            "confidence_score": 0.9,
            "themes": ["Oil"],
            "affected_tickers": [],
            "raw_response": {},
            "source_priority": 90,
            "published_at": datetime(2026, 6, 7, 8, tzinfo=timezone.utc),
        },
        {
            "article_id": "00000000-0000-0000-0000-000000000011",
            "sentiment_score": 0.2,
            "impact_score": 80,
            "confidence_score": 0.9,
            "themes": ["Oil"],
            "affected_tickers": [],
            "raw_response": {},
            "source_priority": 90,
            "published_at": datetime(2026, 6, 7, 12, tzinfo=timezone.utc),
        },
    ]

    signals = aggregate_signal_records(records, window_hours=24, run_time=RUN_TIME)
    signal = next(s for s in signals if s["dimension_value"] == "Oil")

    assert signal["top_article_ids"][0] == "00000000-0000-0000-0000-000000000011"
