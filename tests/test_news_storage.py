from db_builder.news_sources import NewsSource
from db_builder.news_storage import source_record


def test_source_record_uses_stable_source_id():
    source = NewsSource("Google News: AI", "google_news_rss", "https://example.com/rss", category="macro", priority=90)

    assert source_record(source)["source_id"] == source_record(source)["source_id"]
    assert source_record(source)["source_priority"] == 90
    assert source_record(source)["source_category"] == "macro"


def test_bulk_upsert_conflict_sql_mentions_canonical_hash():
    import inspect
    from db_builder.news_storage import upsert_articles

    source = inspect.getsource(upsert_articles)

    assert "ON CONFLICT (canonical_url_hash)" in source
    assert "source_priority" in source
    assert "source_category" in source
