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
