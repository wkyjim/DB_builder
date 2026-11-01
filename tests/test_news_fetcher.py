import feedparser
from pathlib import Path

from db_builder.news_fetcher import clean_html, parse_entry_datetime, parse_feed_entries
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


def test_parse_entry_datetime_handles_iso_and_rfc822():
    # ISO 8601 with Z suffix: the format that produced 59,534 NULL rows.
    d = parse_entry_datetime({"published": "2026-06-05T21:59:42Z"})
    assert d is not None
    assert (d.year, d.month, d.day) == (2026, 6, 5)
    assert d.tzinfo is not None

    # ISO 8601 without timezone (Investing.com style).
    d2 = parse_entry_datetime({"published": "2026-06-08 22:11:39"})
    assert d2 is not None and d2.tzinfo is not None

    # RFC 822 fallback must keep working for RSS 2.0 pubDate.
    d3 = parse_entry_datetime({"published": "Mon, 08 Jun 2026 22:11:39 GMT"})
    assert d3 is not None and d3.year == 2026

    # "updated" is used when "published" is absent.
    d4 = parse_entry_datetime({"updated": "2026-06-07T09:00:00+02:00"})
    assert d4 is not None

    # No date at all stays None rather than raising.
    assert parse_entry_datetime({}) is None
    assert parse_entry_datetime({"published": ""}) is None
    assert parse_entry_datetime({"published": "not a date"}) is None


def test_news_fetch_cli_continues_after_feed_failure():
    source = Path("scripts/news_fetch.py").read_text(encoding="utf-8")

    assert "except Exception as exc" in source
    assert "source_health_failure" in source
    assert "continue" in source
