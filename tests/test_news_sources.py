from db_builder.news_sources import DEFAULT_RSS_SOURCES, google_news_rss_url, sources_for_request


def test_google_news_rss_url_generation():
    url = google_news_rss_url("AI infrastructure")

    assert url == "https://news.google.com/rss/search?q=AI+infrastructure&hl=en-US&gl=US&ceid=US:en"


def test_google_source_requires_keyword_from_request():
    sources = sources_for_request(source="google", keyword="Treasury yields")

    assert len(sources) == 1
    assert sources[0].source_type == "google_news_rss"
    assert "Treasury+yields" in sources[0].feed_url


def test_premium_rss_sources_include_required_institutional_feeds():
    by_name = {source.source_name: source for source in DEFAULT_RSS_SOURCES}

    assert by_name["MarketWatch Top Stories"].feed_url == "https://feeds.content.dowjones.io/public/rss/mw_topstories"
    assert by_name["MarketWatch Top Stories"].priority == 100
    assert by_name["CNBC Markets"].category == "markets"
    assert by_name["Federal Reserve Press Releases"].priority == 100
    assert by_name["Federal Reserve Press Releases"].category == "monetary_policy"
    assert by_name["SEC Press Releases"].priority == 90
    assert by_name["SEC Press Releases"].category == "regulation"
    assert by_name["Reuters Events"].priority == 40


def test_investing_rss_sources_include_required_feeds():
    by_url = {source.feed_url: source for source in DEFAULT_RSS_SOURCES}

    expected = {
        "https://www.investing.com/rss/news_356.rss": ("markets", 85),
        "https://www.investing.com/rss/news_357.rss": ("economy", 92),
        "https://www.investing.com/rss/news_1062.rss": ("commodities", 80),
        "https://www.investing.com/rss/news_1063.rss": ("forex", 80),
        "https://www.investing.com/rss/news_11.rss": ("stock_market", 90),
        "https://www.investing.com/rss/news_25.rss": ("world_news", 90),
        "https://www.investing.com/rss/news_95.rss": ("technology", 85),
        "https://www.investing.com/rss/news_14.rss": ("economic_indicators", 95),
    }

    for url, (category, priority) in expected.items():
        assert by_url[url].category == category
        assert by_url[url].priority == priority


def test_investing_category_filter_returns_economic_indicators_feed():
    sources = sources_for_request(category="economic_indicators")

    assert any(source.source_name == "Investing.com Economic Indicators" for source in sources)
