from db_builder.news_sources import google_news_rss_url, sources_for_request


def test_google_news_rss_url_generation():
    url = google_news_rss_url("AI infrastructure")

    assert url == "https://news.google.com/rss/search?q=AI+infrastructure&hl=en-US&gl=US&ceid=US:en"


def test_google_source_requires_keyword_from_request():
    sources = sources_for_request(source="google", keyword="Treasury yields")

    assert len(sources) == 1
    assert sources[0].source_type == "google_news_rss"
    assert "Treasury+yields" in sources[0].feed_url
