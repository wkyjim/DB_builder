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
        source_name="MarketWatch Top Stories",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/mw_topstories",
        category="markets",
        priority=100,
    ),
    NewsSource(
        source_name="MarketWatch Realtime Headlines",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
        category="markets",
        priority=95,
    ),
    NewsSource(
        source_name="MarketWatch Bulletins",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/mw_bulletins",
        category="markets",
        priority=95,
    ),
    NewsSource(
        source_name="CNBC Markets",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        category="markets",
        priority=95,
    ),
    NewsSource(
        source_name="CNBC Top News",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
        category="macro",
        priority=95,
    ),
    NewsSource(
        source_name="CNBC Technology",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19832390",
        category="technology",
        priority=90,
    ),
    NewsSource(
        source_name="CNBC Economy",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        category="economy",
        priority=90,
    ),
    NewsSource(
        source_name="CNBC Finance",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
        category="finance",
        priority=85,
    ),
    NewsSource(
        source_name="CNBC Investing",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135",
        category="investing",
        priority=85,
    ),
    NewsSource(
        source_name="CNBC Business",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15837362",
        category="business",
        priority=80,
    ),
    NewsSource(
        source_name="CNBC Business News",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19794221",
        category="business",
        priority=80,
    ),
    NewsSource(
        source_name="CNBC Business Headlines",
        source_type="rss",
        feed_url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000113",
        category="business",
        priority=80,
    ),
    NewsSource(
        source_name="Reuters News Releases",
        source_type="rss",
        feed_url="https://ir.thomsonreuters.com/rss/news-releases.xml?items=15",
        category="corporate",
        priority=90,
    ),
    NewsSource(
        source_name="Reuters Events",
        source_type="rss",
        feed_url="https://ir.thomsonreuters.com/rss/events.xml?items=15",
        category="events",
        priority=40,
    ),
    NewsSource(
        source_name="Reuters SEC Filings",
        source_type="rss",
        feed_url="https://ir.thomsonreuters.com/rss/sec-filings.xml?items=15",
        category="sec_filings",
        priority=50,
    ),
    NewsSource(
        source_name="Dow Jones US News",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/RSSUSnews",
        category="macro",
        priority=80,
    ),
    NewsSource(
        source_name="Dow Jones Politics",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed",
        category="politics",
        priority=80,
    ),
    NewsSource(
        source_name="Dow Jones Economy",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
        category="economy",
        priority=85,
    ),
    NewsSource(
        source_name="Dow Jones US Business",
        source_type="rss",
        feed_url="https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
        category="business",
        priority=75,
    ),
    NewsSource(
        source_name="Federal Reserve Press Releases",
        source_type="rss",
        feed_url="https://www.federalreserve.gov/feeds/press_all.xml",
        category="monetary_policy",
        priority=100,
    ),
    NewsSource(
        source_name="Federal Reserve Speeches",
        source_type="rss",
        feed_url="https://www.federalreserve.gov/feeds/speeches.xml",
        category="central_banks",
        priority=95,
    ),
    NewsSource(
        source_name="SEC Press Releases",
        source_type="rss",
        feed_url="https://www.sec.gov/news/pressreleases.rss",
        category="regulation",
        priority=90,
    ),
    NewsSource(
        source_name="SEC Speeches and Statements",
        source_type="rss",
        feed_url="https://www.sec.gov/news/speeches-statements.rss",
        category="regulation",
        priority=85,
    ),
    NewsSource(
        source_name="Yahoo Finance",
        source_type="rss",
        feed_url="https://finance.yahoo.com/news/rssindex",
        category="markets",
        priority=50,
    ),
    NewsSource(
        source_name="Investing.com Markets",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_356.rss",
        category="markets",
        priority=85,
    ),
    NewsSource(
        source_name="Investing.com Economy",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_357.rss",
        category="economy",
        priority=92,
    ),
    NewsSource(
        source_name="Investing.com Commodities",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_1062.rss",
        category="commodities",
        priority=80,
    ),
    NewsSource(
        source_name="Investing.com Forex",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_1063.rss",
        category="forex",
        priority=80,
    ),
    NewsSource(
        source_name="Investing.com Stock Market",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_11.rss",
        category="stock_market",
        priority=90,
    ),
    NewsSource(
        source_name="Investing.com World News",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_25.rss",
        category="world_news",
        priority=90,
    ),
    NewsSource(
        source_name="Investing.com Technology",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_95.rss",
        category="technology",
        priority=85,
    ),
    NewsSource(
        source_name="Investing.com Economic Indicators",
        source_type="rss",
        feed_url="https://www.investing.com/rss/news_14.rss",
        category="economic_indicators",
        priority=95,
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
