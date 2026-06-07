"""Local PostgreSQL storage for the news intelligence pipeline."""

from __future__ import annotations

import json

from sqlalchemy import text

from db_builder.news_keywords import seed_keyword_records
from db_builder.news_sources import NewsSource


def setup_news_schema(engine) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS public.news_sources (
        source_id uuid PRIMARY KEY,
        source_name text NOT NULL,
        source_type text NOT NULL,
        feed_url text NOT NULL,
        category text,
        priority integer,
        source_priority integer,
        source_category text,
        enabled boolean,
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS public.news_articles (
        article_id uuid PRIMARY KEY,
        source_name text,
        source_type text,
        feed_url text,
        source_priority integer,
        source_category text,
        title text,
        summary text,
        url text,
        canonical_url text,
        canonical_url_hash text UNIQUE,
        title_hash text,
        published_at timestamptz,
        fetched_at timestamptz,
        language text,
        matched_keywords text[],
        related_tickers text[],
        classification_status text DEFAULT 'new',
        classification_attempts integer DEFAULT 0,
        last_classification_error text,
        raw_payload jsonb
    );

    CREATE TABLE IF NOT EXISTS public.news_classifications (
        article_id uuid PRIMARY KEY,
        model_provider text,
        model_name text,
        classified_at timestamptz,
        sentiment_score numeric,
        impact_score numeric,
        confidence_score numeric,
        themes text[],
        event_type text,
        time_horizon text,
        affected_tickers text[],
        reasoning text,
        raw_response jsonb
    );

    CREATE TABLE IF NOT EXISTS public.news_keywords (
        keyword_id uuid PRIMARY KEY,
        theme text,
        subtheme text,
        keyword text UNIQUE,
        priority integer,
        enabled boolean,
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS public.news_keyword_candidates (
        candidate_id uuid PRIMARY KEY,
        keyword text,
        theme text,
        subtheme text,
        source_headline_count integer,
        example_headlines text[],
        confidence_score numeric,
        first_seen_at timestamptz,
        last_seen_at timestamptz,
        status text,
        review_reason text
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_news_articles_canonical_url_hash
    ON public.news_articles (canonical_url_hash);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_news_keywords_keyword
    ON public.news_keywords (keyword);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_news_keyword_candidates_keyword
    ON public.news_keyword_candidates (keyword);

    ALTER TABLE public.news_sources
    ADD COLUMN IF NOT EXISTS source_priority integer;

    ALTER TABLE public.news_sources
    ADD COLUMN IF NOT EXISTS source_category text;

    ALTER TABLE public.news_articles
    ADD COLUMN IF NOT EXISTS source_priority integer;

    ALTER TABLE public.news_articles
    ADD COLUMN IF NOT EXISTS source_category text;
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def source_record(source: NewsSource) -> dict:
    import uuid

    return {
        "source_id": str(uuid.uuid5(uuid.NAMESPACE_URL, source.feed_url)),
        "source_name": source.source_name,
        "source_type": source.source_type,
        "feed_url": source.feed_url,
        "category": source.category,
        "priority": source.priority,
        "source_priority": source.priority,
        "source_category": source.category,
        "enabled": source.enabled,
    }


def upsert_sources(engine, sources: list[NewsSource]) -> None:
    if not sources:
        return
    sql = text("""
        INSERT INTO public.news_sources (
            source_id, source_name, source_type, feed_url, category, priority,
            source_priority, source_category, enabled, created_at, updated_at
        )
        VALUES (
            :source_id, :source_name, :source_type, :feed_url, :category, :priority,
            :source_priority, :source_category, :enabled, now(), now()
        )
        ON CONFLICT (source_id)
        DO UPDATE SET
            source_name = EXCLUDED.source_name,
            source_type = EXCLUDED.source_type,
            feed_url = EXCLUDED.feed_url,
            category = EXCLUDED.category,
            priority = EXCLUDED.priority,
            source_priority = EXCLUDED.source_priority,
            source_category = EXCLUDED.source_category,
            enabled = EXCLUDED.enabled,
            updated_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, [source_record(source) for source in sources])


def seed_news_keywords(engine) -> None:
    sql = text("""
        INSERT INTO public.news_keywords (
            keyword_id, theme, subtheme, keyword, priority, enabled, created_at, updated_at
        )
        VALUES (
            :keyword_id, :theme, :subtheme, :keyword, :priority, :enabled, now(), now()
        )
        ON CONFLICT (keyword)
        DO UPDATE SET
            theme = EXCLUDED.theme,
            subtheme = EXCLUDED.subtheme,
            priority = EXCLUDED.priority,
            enabled = EXCLUDED.enabled,
            updated_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, seed_keyword_records())


def serialize_article(article: dict) -> dict:
    row = article.copy()
    row["raw_payload"] = json.dumps(row.get("raw_payload") or {}, default=str)
    return row


def upsert_articles(engine, articles: list[dict]) -> None:
    if not articles:
        return
    sql = text("""
        INSERT INTO public.news_articles (
            article_id, source_name, source_type, feed_url, title, summary, url,
            source_priority, source_category, canonical_url, canonical_url_hash, title_hash, published_at, fetched_at,
            language, matched_keywords, related_tickers, classification_status,
            classification_attempts, last_classification_error, raw_payload
        )
        VALUES (
            :article_id, :source_name, :source_type, :feed_url, :title, :summary, :url,
            :source_priority, :source_category, :canonical_url, :canonical_url_hash, :title_hash, :published_at, :fetched_at,
            :language, :matched_keywords, :related_tickers, :classification_status,
            :classification_attempts, :last_classification_error, CAST(:raw_payload AS jsonb)
        )
        ON CONFLICT (canonical_url_hash)
        DO UPDATE SET
            source_name = EXCLUDED.source_name,
            source_type = EXCLUDED.source_type,
            feed_url = EXCLUDED.feed_url,
            source_priority = EXCLUDED.source_priority,
            source_category = EXCLUDED.source_category,
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            url = EXCLUDED.url,
            canonical_url = EXCLUDED.canonical_url,
            title_hash = EXCLUDED.title_hash,
            published_at = EXCLUDED.published_at,
            fetched_at = EXCLUDED.fetched_at,
            language = EXCLUDED.language,
            matched_keywords = EXCLUDED.matched_keywords,
            related_tickers = EXCLUDED.related_tickers,
            raw_payload = EXCLUDED.raw_payload;
    """)
    with engine.begin() as conn:
        conn.execute(sql, [serialize_article(article) for article in articles])


def upsert_keyword_candidates(engine, candidates: list[dict]) -> None:
    if not candidates:
        return
    sql = text("""
        INSERT INTO public.news_keyword_candidates (
            candidate_id, keyword, theme, subtheme, source_headline_count,
            example_headlines, confidence_score, first_seen_at, last_seen_at,
            status, review_reason
        )
        VALUES (
            :candidate_id, :keyword, :theme, :subtheme, :source_headline_count,
            :example_headlines, :confidence_score, :first_seen_at, :last_seen_at,
            :status, :review_reason
        )
        ON CONFLICT (keyword)
        DO UPDATE SET
            source_headline_count = EXCLUDED.source_headline_count,
            example_headlines = EXCLUDED.example_headlines,
            confidence_score = EXCLUDED.confidence_score,
            last_seen_at = EXCLUDED.last_seen_at,
            status = EXCLUDED.status,
            review_reason = EXCLUDED.review_reason;
    """)
    with engine.begin() as conn:
        conn.execute(sql, candidates)
