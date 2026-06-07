"""RSS source helpers for the local news intelligence pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus


GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"


@dataclass(frozen=True)
class NewsSource:
    source_name: str
    source_type: str
    feed_url: str
    category: str = "general"
    priority: int = 100
    enabled: bool = True


DEFAULT_RSS_SOURCES = [
    NewsSource(
        source_name="Yahoo Finance",
        source_type="rss",
        feed_url="https://finance.yahoo.com/news/rssindex",
        category="markets",
        priority=50,
    ),
]


def google_news_rss_url(keyword: str, *, hl: str = "en-US", gl: str = "US", ceid: str = "US:en") -> str:
    query = quote_plus(keyword.strip())
    return f"{GOOGLE_NEWS_RSS_BASE}?q={query}&hl={hl}&gl={gl}&ceid={ceid}"


def google_news_source(keyword: str, *, category: str = "keyword", priority: int = 75) -> NewsSource:
    clean_keyword = keyword.strip()
    return NewsSource(
        source_name=f"Google News: {clean_keyword}",
        source_type="google_news_rss",
        feed_url=google_news_rss_url(clean_keyword),
        category=category,
        priority=priority,
    )


def sources_for_request(
    *,
    source: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
) -> list[NewsSource]:
    if keyword:
        return [google_news_source(keyword, category=category or "keyword")]

    if source and source.lower() == "google":
        raise ValueError("--source google requires --keyword")

    sources = DEFAULT_RSS_SOURCES
    if source:
        sources = [s for s in sources if s.source_name.lower() == source.lower()]
    if category:
        sources = [s for s in sources if s.category.lower() == category.lower()]

    return [s for s in sources if s.enabled]
