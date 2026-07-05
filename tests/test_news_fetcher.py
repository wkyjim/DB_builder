import feedparser
from pathlib import Path

from db_builder.news_fetcher import clean_html, parse_feed_entries
from db_builder.news_sources import NewsSource


RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>AI infrastructure lifts Nvidia</title>
      <link>https://example.com/a?utm_source=test</link>
      <description><![CDATA[<p>GPU demand rises.</p>]]></description>
      <pubDate>Sat, 06 Jun 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_clean_html_removes_tags():
    assert clean_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_parse_feed_entries_normalizes_article():
    source = NewsSource("Test", "rss", "https://example.com/rss", category="markets", priority=95)
    articles = parse_feed_entries(feedparser.parse(RSS), source)

    assert len(articles) == 1
    assert articles[0]["title"] == "AI infrastructure lifts Nvidia"
    assert articles[0]["summary"] == "GPU demand rises."
    assert articles[0]["canonical_url"] == "https://example.com/a"
    assert "AI" in articles[0]["matched_keywords"]
    assert articles[0]["source_priority"] == 95
    assert articles[0]["source_category"] == "markets"


def test_news_fetch_cli_continues_after_feed_failure():
    source = Path("scripts/news_fetch.py").read_text(encoding="utf-8")

    assert "except Exception as exc" in source
    assert "source_health_failure" in source
    assert "continue" in source
