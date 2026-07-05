"""RSS source health tracking for the local news intelligence pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text


@dataclass(frozen=True)
class SourceFetchHealthEvent:
    source_name: str
    feed_url: str
    succeeded: bool
    fetch_seconds: float
    article_count: int = 0
    error: str | None = None
    event_time: datetime | None = None

    def as_row(self) -> dict:
        event_time = self.event_time or datetime.now(timezone.utc)
        return {
            "source_name": self.source_name,
            "feed_url": self.feed_url,
            "succeeded": self.succeeded,
            "event_time": event_time,
            "error": self.error,
            "fetch_seconds": self.fetch_seconds,
            "article_count": self.article_count,
        }


def source_health_success(
    *,
    source_name: str,
    feed_url: str,
    fetch_seconds: float,
    article_count: int,
) -> SourceFetchHealthEvent:
    return SourceFetchHealthEvent(
        source_name=source_name,
        feed_url=feed_url,
        succeeded=True,
        fetch_seconds=fetch_seconds,
        article_count=article_count,
    )


def source_health_failure(
    *,
    source_name: str,
    feed_url: str,
    fetch_seconds: float,
    error: str,
) -> SourceFetchHealthEvent:
    return SourceFetchHealthEvent(
        source_name=source_name,
        feed_url=feed_url,
        succeeded=False,
        fetch_seconds=fetch_seconds,
        error=error,
    )


def setup_source_health_schema(engine) -> None:
    sql = text("""
        CREATE TABLE IF NOT EXISTS public.news_source_health (
            source_name text NOT NULL,
            feed_url text PRIMARY KEY,
            last_success_at timestamptz,
            last_failure_at timestamptz,
            success_count integer DEFAULT 0,
            failure_count integer DEFAULT 0,
            consecutive_failure_count integer DEFAULT 0,
            last_error text,
            last_fetch_seconds numeric,
            avg_fetch_seconds numeric,
            avg_response_time numeric,
            last_article_count integer DEFAULT 0,
            enabled_recommendation boolean DEFAULT true,
            latest_status text,
            updated_at timestamptz DEFAULT now()
        );
    """)
    with engine.begin() as conn:
        conn.execute(sql)
        conn.execute(text("ALTER TABLE public.news_source_health ADD COLUMN IF NOT EXISTS avg_response_time numeric;"))
        conn.execute(text("ALTER TABLE public.news_source_health ADD COLUMN IF NOT EXISTS latest_status text;"))


def upsert_source_health_events(engine, events: list[SourceFetchHealthEvent]) -> None:
    if not events:
        return

    sql = text("""
        INSERT INTO public.news_source_health (
            source_name, feed_url, last_success_at, last_failure_at,
            success_count, failure_count, consecutive_failure_count, last_error,
            last_fetch_seconds, avg_fetch_seconds, avg_response_time, last_article_count,
            enabled_recommendation, latest_status, updated_at
        )
        VALUES (
            :source_name,
            :feed_url,
            CASE WHEN :succeeded THEN :event_time ELSE NULL END,
            CASE WHEN :succeeded THEN NULL ELSE :event_time END,
            CASE WHEN :succeeded THEN 1 ELSE 0 END,
            CASE WHEN :succeeded THEN 0 ELSE 1 END,
            CASE WHEN :succeeded THEN 0 ELSE 1 END,
            CASE WHEN :succeeded THEN NULL ELSE :error END,
            :fetch_seconds,
            :fetch_seconds,
            :fetch_seconds,
            :article_count,
            CASE WHEN :succeeded THEN true ELSE true END,
            CASE WHEN :succeeded THEN 'success' ELSE 'failure' END,
            now()
        )
        ON CONFLICT (feed_url)
        DO UPDATE SET
            source_name = EXCLUDED.source_name,
            last_success_at = CASE
                WHEN :succeeded THEN :event_time
                ELSE public.news_source_health.last_success_at
            END,
            last_failure_at = CASE
                WHEN :succeeded THEN public.news_source_health.last_failure_at
                ELSE :event_time
            END,
            success_count = public.news_source_health.success_count + CASE WHEN :succeeded THEN 1 ELSE 0 END,
            failure_count = public.news_source_health.failure_count + CASE WHEN :succeeded THEN 0 ELSE 1 END,
            consecutive_failure_count = CASE
                WHEN :succeeded THEN 0
                ELSE public.news_source_health.consecutive_failure_count + 1
            END,
            last_error = CASE WHEN :succeeded THEN NULL ELSE :error END,
            last_fetch_seconds = :fetch_seconds,
            avg_fetch_seconds = CASE
                WHEN public.news_source_health.avg_fetch_seconds IS NULL THEN :fetch_seconds
                ELSE ((public.news_source_health.avg_fetch_seconds * GREATEST(public.news_source_health.success_count + public.news_source_health.failure_count, 1)) + :fetch_seconds)
                    / GREATEST(public.news_source_health.success_count + public.news_source_health.failure_count + 1, 1)
            END,
            avg_response_time = CASE
                WHEN public.news_source_health.avg_response_time IS NULL THEN :fetch_seconds
                ELSE ((public.news_source_health.avg_response_time * GREATEST(public.news_source_health.success_count + public.news_source_health.failure_count, 1)) + :fetch_seconds)
                    / GREATEST(public.news_source_health.success_count + public.news_source_health.failure_count + 1, 1)
            END,
            last_article_count = :article_count,
            enabled_recommendation = CASE
                WHEN :succeeded THEN true
                WHEN public.news_source_health.consecutive_failure_count + 1 >= 5 THEN false
                ELSE public.news_source_health.enabled_recommendation
            END,
            latest_status = CASE WHEN :succeeded THEN 'success' ELSE 'failure' END,
            updated_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, [event.as_row() for event in events])


def fetch_source_health_summary(engine, *, limit: int = 50) -> list[dict]:
    sql = text("""
        SELECT
            source_name,
            feed_url,
            success_count,
            failure_count,
            consecutive_failure_count,
            last_success_at,
            last_failure_at,
            last_fetch_seconds,
            avg_fetch_seconds,
            avg_response_time,
            last_article_count,
            enabled_recommendation,
            latest_status,
            last_error
        FROM public.news_source_health
        ORDER BY enabled_recommendation ASC, consecutive_failure_count DESC, failure_count DESC, source_name
        LIMIT :limit;
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"limit": limit}).mappings().all()
    return [dict(row) for row in rows]
