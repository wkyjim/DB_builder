from db_builder.news_keyword_discovery import generate_keyword_candidates
from db_builder.news_keywords import seed_keyword_records


def test_seed_keywords_include_required_macro_and_technology_terms():
    keywords = {row["keyword"] for row in seed_keyword_records()}

    assert "Federal Reserve" in keywords
    assert "Treasury yields" in keywords
    assert "AI" in keywords
    assert "uranium" in keywords
    assert "monetary policy" in keywords
    assert "bank regulation" in keywords
    assert "ETF regulation" in keywords
    assert "crypto regulation" in keywords
    assert "trade policy" in keywords


def test_keyword_candidate_generation_from_low_coverage_headlines():
    articles = [
        {"title": "Quantum computing demand rises", "matched_keywords": []},
        {"title": "Quantum computing stocks rally", "matched_keywords": []},
        {"title": "Quantum computing supply chain expands", "matched_keywords": []},
    ]

    candidates = generate_keyword_candidates(articles, min_count=2)
    keywords = {candidate["keyword"] for candidate in candidates}

    assert "quantum computing" in keywords


def test_keyword_discovery_rejects_generic_candidates():
    articles = [
        {"title": "Investors should think twice as stock underperforming today", "matched_keywords": []},
        {"title": "Investors should think about market reports this year", "matched_keywords": []},
        {"title": "Company shares said investors should wait", "matched_keywords": []},
    ]

    candidates = generate_keyword_candidates(articles, min_count=1)
    keywords = {candidate["keyword"] for candidate in candidates}

    assert "investors" not in keywords
    assert "should" not in keywords
    assert "think" not in keywords
    assert "should think" not in keywords
    assert "underperforming" not in keywords


def test_keyword_discovery_keeps_financial_theme_candidates():
    articles = [
        {"title": "SpaceX futures rise as Treasury yields fall", "matched_keywords": []},
        {"title": "Defense spending boosts AI infrastructure demand", "matched_keywords": []},
        {"title": "Treasury yields and defense spending shape futures", "matched_keywords": []},
    ]

    candidates = generate_keyword_candidates(articles, min_count=1)
    keywords = {candidate["keyword"] for candidate in candidates}

    assert "spacex" in keywords
    assert "futures" in keywords
    assert "treasury yields" in keywords
    assert "defense spending" in keywords
    assert "ai infrastructure" in keywords
