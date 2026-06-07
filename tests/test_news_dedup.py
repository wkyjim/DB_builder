from datetime import datetime, timezone

from db_builder.news_dedup import article_hashes, canonicalize_url, title_fallback_hash


def test_canonicalize_url_removes_tracking_and_normalizes_host():
    url = "HTTPS://Example.COM/path/?utm_source=x&b=2&a=1&fbclid=abc#frag"

    assert canonicalize_url(url) == "https://example.com/path?a=1&b=2"


def test_article_hashes_fallback_to_title_when_url_missing():
    published = datetime(2026, 6, 6, tzinfo=timezone.utc)
    canonical_url, canonical_hash, title_hash = article_hashes("", "Fed cuts rates", "Source", published)

    assert canonical_url == ""
    assert canonical_hash == title_hash
    assert title_hash == title_fallback_hash("Fed cuts rates", "Source", published)


def test_duplicate_tracking_urls_hash_same():
    first = article_hashes("https://example.com/a?utm_campaign=x&id=1", "Title", "S", None)
    second = article_hashes("https://example.com/a?id=1", "Title", "S", None)

    assert first[1] == second[1]
