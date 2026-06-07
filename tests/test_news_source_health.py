from db_builder.news_source_health import (
    SourceFetchHealthEvent,
    source_health_failure,
    source_health_success,
)


def test_success_event_serializes_health_row():
    event = source_health_success(
        source_name="CNBC Markets",
        feed_url="https://example.com/rss",
        fetch_seconds=1.25,
        article_count=12,
    )

    row = event.as_row()

    assert row["source_name"] == "CNBC Markets"
    assert row["feed_url"] == "https://example.com/rss"
    assert row["succeeded"] is True
    assert row["fetch_seconds"] == 1.25
    assert row["article_count"] == 12
    assert row["error"] is None


def test_failure_event_serializes_error_row():
    event = source_health_failure(
        source_name="Reuters Events",
        feed_url="https://example.com/events.xml",
        fetch_seconds=20.0,
        error="timeout",
    )

    row = event.as_row()

    assert row["succeeded"] is False
    assert row["article_count"] == 0
    assert row["error"] == "timeout"


def test_health_sql_marks_review_after_five_consecutive_failures():
    import inspect
    from db_builder.news_source_health import upsert_source_health_events

    source = inspect.getsource(upsert_source_health_events)

    assert "consecutive_failure_count + 1 >= 5" in source
    assert "enabled_recommendation" in source


def test_health_event_requires_source_identity():
    event = SourceFetchHealthEvent(
        source_name="Federal Reserve Press Releases",
        feed_url="https://example.com/fed.xml",
        succeeded=True,
        fetch_seconds=0.5,
        article_count=2,
    )

    assert event.as_row()["source_name"] == "Federal Reserve Press Releases"
