"""RSS fetching and normalization for investment news."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
from bs4 import BeautifulSoup

from db_builder.news_dedup import article_hashes, normalize_text
from db_builder.news_keywords import enabled_keyword_lookup
from db_builder.news_sources import NewsSource


TICKER_RE = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b")


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return normalize_text(text)


def parse_entry_datetime(entry: dict):
    value = entry.get("published") or entry.get("updated")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def fetch_rss(feed_url: str, *, timeout: int = 20):
    response = requests.get(feed_url, timeout=timeout, headers={"User-Agent": "DB_builder news fetcher"})
    response.raise_for_status()
    return feedparser.parse(response.content)


def match_keywords(text: str, keyword_records: list[dict] | None = None) -> list[str]:
    lookup = enabled_keyword_lookup(keyword_records)
    lowered = text.lower()
    matched = [record["keyword"] for key, record in lookup.items() if key in lowered]
    return sorted(set(matched), key=str.lower)


def extract_related_tickers(text: str) -> list[str]:
    blocked = {"AI", "CEO", "CFO", "CPI", "ECB", "FDA", "GDP", "LLM", "NFP", "PMI", "PPI", "SEC", "USA", "USD"}
    tickers = [match.group(0) for match in TICKER_RE.finditer(text)]
    return sorted({ticker for ticker in tickers if ticker not in blocked})


def normalize_entry(entry: dict, source: NewsSource, *, keyword_records: list[dict] | None = None) -> dict:
    title = normalize_text(clean_html(entry.get("title")))
    summary = clean_html(entry.get("summary") or entry.get("description"))
    url = entry.get("link") or ""
    published_at = parse_entry_datetime(entry)
    fetched_at = datetime.now(timezone.utc)
    canonical_url, canonical_url_hash, title_hash = article_hashes(
        url,
        title,
        source.source_name,
        published_at,
    )
    text = f"{title} {summary}"
    matched_keywords = match_keywords(text, keyword_records)
    related_tickers = extract_related_tickers(text)

    return {
        "article_id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_url_hash)),
        "source_name": source.source_name,
        "source_type": source.source_type,
        "feed_url": source.feed_url,
        "source_priority": source.priority,
        "source_category": source.category,
        "title": title,
        "summary": summary,
        "url": url,
        "canonical_url": canonical_url,
        "canonical_url_hash": canonical_url_hash,
        "title_hash": title_hash,
        "published_at": published_at,
        "fetched_at": fetched_at,
        "language": "en",
        "matched_keywords": matched_keywords,
        "related_tickers": related_tickers,
        "classification_status": "new",
        "classification_attempts": 0,
        "last_classification_error": None,
        "raw_payload": dict(entry),
    }


def parse_feed_entries(feed, source: NewsSource, *, limit: int | None = None, keyword_records: list[dict] | None = None) -> list[dict]:
    entries = feed.entries[:limit] if limit else feed.entries
    articles = [normalize_entry(dict(entry), source, keyword_records=keyword_records) for entry in entries]
    seen = set()
    unique_articles = []
    for article in articles:
        key = article["canonical_url_hash"]
        if key in seen:
            continue
        seen.add(key)
        unique_articles.append(article)
    return unique_articles


def fetch_source_articles(source: NewsSource, *, limit: int | None = None, timeout: int = 20, keyword_records: list[dict] | None = None) -> list[dict]:
    feed = fetch_rss(source.feed_url, timeout=timeout)
    return parse_feed_entries(feed, source, limit=limit, keyword_records=keyword_records)
