"""Aggregate classified news into investable local signals."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import text

from db_builder.news_taxonomy import canonical_theme, normalize_ticker


DEFAULT_WINDOWS = [6, 24, 72]
HIGH_IMPACT_THRESHOLD = 70
DEFAULT_SOURCE_PRIORITY = 75


def setup_news_signals_schema(engine) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS public.news_signals (
        signal_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        dimension_type text,
        dimension_value text,
        article_count integer,
        high_impact_count integer,
        avg_sentiment_score numeric,
        weighted_sentiment_score numeric,
        avg_impact_score numeric,
        avg_confidence_score numeric,
        positive_count integer,
        negative_count integer,
        neutral_count integer,
        opportunity_score numeric,
        risk_score numeric,
        top_article_ids uuid[],
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_news_signals_run_dimension
    ON public.news_signals (run_time, window_hours, dimension_type, dimension_value);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def _table_columns(engine, table_name: str) -> set[str]:
    schema, table = table_name.split(".", 1)
    sql = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
    """)
    with engine.begin() as conn:
        rows = conn.execute(sql, {"schema": schema, "table": table}).fetchall()
    return {row[0] for row in rows}


def fetch_classified_news(engine, *, window_hours: int, run_time: datetime | None = None) -> pd.DataFrame:
    selected_run_time = run_time or datetime.now(timezone.utc)
    article_columns = _table_columns(engine, "public.news_articles")
    source_columns = _table_columns(engine, "public.news_sources")
    priority_exprs = []
    category_exprs = []
    if "source_priority" in article_columns:
        priority_exprs.append("a.source_priority")
    if "source_priority" in source_columns:
        priority_exprs.append("s.source_priority")
    if "priority" in source_columns:
        priority_exprs.append("s.priority")
    if "source_category" in article_columns:
        category_exprs.append("a.source_category")
    if "source_category" in source_columns:
        category_exprs.append("s.source_category")
    if "category" in source_columns:
        category_exprs.append("s.category")

    source_priority_sql = "COALESCE(" + ", ".join(priority_exprs + [":default_source_priority"]) + ")"
    source_category_sql = (
        "COALESCE(" + ", ".join(category_exprs + ["NULL"]) + ")"
        if category_exprs
        else "NULL"
    )
    sql = text("""
        SELECT
            c.article_id,
            c.sentiment_score,
            c.impact_score,
            c.confidence_score,
            c.themes,
            c.affected_tickers,
            c.raw_response,
            {source_priority_sql} AS source_priority,
            {source_category_sql} AS source_category,
            a.source_name,
            a.published_at,
            a.fetched_at
        FROM public.news_classifications c
        JOIN public.news_articles a ON a.article_id = c.article_id
        LEFT JOIN public.news_sources s ON s.feed_url = a.feed_url
        WHERE COALESCE(a.published_at, a.fetched_at, c.classified_at)
            >= (CAST(:run_time AS timestamptz) - (:window_hours * INTERVAL '1 hour'))
    """.format(source_priority_sql=source_priority_sql, source_category_sql=source_category_sql))
    return pd.read_sql(
        sql,
        engine,
        params={
            "run_time": selected_run_time,
            "window_hours": window_hours,
            "default_source_priority": DEFAULT_SOURCE_PRIORITY,
        },
    )


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def _raw_response_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _optional_dimension_values(record: dict, key: str) -> list[str]:
    raw = _raw_response_dict(record.get("raw_response"))
    values = raw.get(key)
    if values is None and isinstance(raw.get("initial_response"), dict):
        values = raw["initial_response"].get(key)
    return [str(value).strip() for value in _as_list(values) if str(value).strip()]


def expand_signal_dimensions(record: dict) -> list[tuple[str, str]]:
    dimensions = []
    for theme in _as_list(record.get("themes")):
        value = canonical_theme(str(theme))
        if value:
            dimensions.append(("theme", value))
    for ticker in _as_list(record.get("affected_tickers")):
        value = normalize_ticker(str(ticker))
        if value:
            dimensions.append(("ticker", value))
    for asset_class in _optional_dimension_values(record, "asset_classes"):
        dimensions.append(("asset_class", asset_class))
    for region in _optional_dimension_values(record, "regions"):
        dimensions.append(("region", region))

    unique_dimensions = []
    seen = set()
    for dimension in dimensions:
        if dimension in seen:
            continue
        seen.add(dimension)
        unique_dimensions.append(dimension)
    return unique_dimensions


def _signal_id(run_time: datetime, window_hours: int, dimension_type: str, dimension_value: str) -> str:
    value = "|".join([run_time.isoformat(), str(window_hours), dimension_type, dimension_value.lower()])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.news_signal:{value}"))


def _weighted_sentiment(records: list[dict]) -> float:
    numerator = sum(
        float(r["sentiment_score"])
        * float(r["impact_score"])
        * float(r["confidence_score"])
        * source_weight_factor(r)
        for r in records
    )
    denominator = sum(
        float(r["impact_score"]) * float(r["confidence_score"]) * source_weight_factor(r)
        for r in records
    )
    return 0.0 if denominator == 0 else numerator / denominator


def source_weight_factor(record: dict) -> float:
    priority = record.get("source_priority")
    try:
        numeric = float(priority)
    except (TypeError, ValueError):
        numeric = DEFAULT_SOURCE_PRIORITY
    numeric = min(max(numeric, 0), 100)
    return numeric / 100.0


def _score(weighted_sentiment: float, high_impact_count: int, article_count: int, *, positive: bool) -> float:
    sentiment_component = max(weighted_sentiment, 0) if positive else max(-weighted_sentiment, 0)
    return round((sentiment_component * 60) + (high_impact_count * 10) + min(article_count, 10) * 2, 4)


def aggregate_signal_records(
    records: list[dict] | pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> list[dict]:
    selected_run_time = run_time or datetime.now(timezone.utc)
    source = records.to_dict(orient="records") if isinstance(records, pd.DataFrame) else records
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for record in source:
        for dimension in expand_signal_dimensions(record):
            groups[dimension].append(record)

    signals = []
    for (dimension_type, dimension_value), grouped in groups.items():
        article_count = len(grouped)
        high_impact_count = sum(float(r["impact_score"]) >= HIGH_IMPACT_THRESHOLD for r in grouped)
        avg_sentiment = sum(float(r["sentiment_score"]) for r in grouped) / article_count
        avg_impact = sum(float(r["impact_score"]) for r in grouped) / article_count
        avg_confidence = sum(float(r["confidence_score"]) for r in grouped) / article_count
        weighted = _weighted_sentiment(grouped)
        positive_count = sum(float(r["sentiment_score"]) > 0.15 for r in grouped)
        negative_count = sum(float(r["sentiment_score"]) < -0.15 for r in grouped)
        neutral_count = article_count - positive_count - negative_count
        top_articles = sorted(
            grouped,
            key=lambda r: float(r["impact_score"]) * float(r["confidence_score"]) * source_weight_factor(r),
            reverse=True,
        )[:5]

        signals.append(
            {
                "signal_id": _signal_id(selected_run_time, window_hours, dimension_type, dimension_value),
                "run_time": selected_run_time,
                "window_hours": window_hours,
                "dimension_type": dimension_type,
                "dimension_value": dimension_value,
                "article_count": article_count,
                "high_impact_count": high_impact_count,
                "avg_sentiment_score": round(avg_sentiment, 6),
                "weighted_sentiment_score": round(weighted, 6),
                "avg_impact_score": round(avg_impact, 6),
                "avg_confidence_score": round(avg_confidence, 6),
                "positive_count": positive_count,
                "negative_count": negative_count,
                "neutral_count": neutral_count,
                "opportunity_score": _score(weighted, high_impact_count, article_count, positive=True),
                "risk_score": _score(weighted, high_impact_count, article_count, positive=False),
                "top_article_ids": [str(article["article_id"]) for article in top_articles],
            }
        )

    return sorted(
        signals,
        key=lambda r: (-max(r["opportunity_score"], r["risk_score"]), r["dimension_type"], r["dimension_value"]),
    )


def upsert_news_signals(engine, signals: list[dict]) -> None:
    if not signals:
        return
    sql = text("""
        INSERT INTO public.news_signals (
            signal_id, run_time, window_hours, dimension_type, dimension_value,
            article_count, high_impact_count, avg_sentiment_score,
            weighted_sentiment_score, avg_impact_score, avg_confidence_score,
            positive_count, negative_count, neutral_count, opportunity_score,
            risk_score, top_article_ids, created_at
        )
        VALUES (
            :signal_id, :run_time, :window_hours, :dimension_type, :dimension_value,
            :article_count, :high_impact_count, :avg_sentiment_score,
            :weighted_sentiment_score, :avg_impact_score, :avg_confidence_score,
            :positive_count, :negative_count, :neutral_count, :opportunity_score,
            :risk_score, CAST(:top_article_ids AS uuid[]), now()
        )
        ON CONFLICT (signal_id)
        DO UPDATE SET
            article_count = EXCLUDED.article_count,
            high_impact_count = EXCLUDED.high_impact_count,
            avg_sentiment_score = EXCLUDED.avg_sentiment_score,
            weighted_sentiment_score = EXCLUDED.weighted_sentiment_score,
            avg_impact_score = EXCLUDED.avg_impact_score,
            avg_confidence_score = EXCLUDED.avg_confidence_score,
            positive_count = EXCLUDED.positive_count,
            negative_count = EXCLUDED.negative_count,
            neutral_count = EXCLUDED.neutral_count,
            opportunity_score = EXCLUDED.opportunity_score,
            risk_score = EXCLUDED.risk_score,
            top_article_ids = EXCLUDED.top_article_ids,
            created_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, signals)


def generate_news_signals(engine, *, window_hours: int, dry_run: bool = True) -> list[dict]:
    run_time = datetime.now(timezone.utc)
    df = fetch_classified_news(engine, window_hours=window_hours, run_time=run_time)
    signals = aggregate_signal_records(df, window_hours=window_hours, run_time=run_time)
    if not dry_run:
        setup_news_signals_schema(engine)
        upsert_news_signals(engine, signals)
    return signals
