"""Local Ollama/DeepSeek classifier for stored news articles."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests
from sqlalchemy import text

from db_builder.news_importance import sort_classification_queue


DEFAULT_OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_OLLAMA_MODEL = "deepseek-r1:14b"
REQUIRED_FIELDS = {
    "sentiment_score",
    "impact_score",
    "confidence_score",
    "themes",
    "event_type",
    "time_horizon",
    "affected_tickers",
    "reasoning",
}
DEFAULT_FIELDS = {
    "reasoning": "",
    "themes": [],
    "affected_tickers": [],
}


class ClassificationError(ValueError):
    pass


def ollama_url() -> str:
    return os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL)


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def select_articles_for_classification(
    engine,
    *,
    limit: int | None = None,
    article_id: str | None = None,
    min_importance: float | None = None,
    source_priority_min: float | None = None,
    lookback_hours: float | None = None,
    source_category: str | None = None,
) -> list[dict]:
    article_clause = "AND article_id = :article_id" if article_id else ""
    source_priority_clause = "AND COALESCE(a.source_priority, s.source_priority, s.priority, :default_source_priority) >= :source_priority_min" if source_priority_min is not None else ""
    lookback_clause = "AND COALESCE(a.published_at, a.fetched_at) >= now() - (:lookback_hours * INTERVAL '1 hour')" if lookback_hours is not None else ""
    source_category_clause = "AND COALESCE(a.source_category, s.source_category, s.category) = :source_category" if source_category else ""
    params: dict[str, Any] = {}
    if article_id:
        params["article_id"] = article_id
    if limit is not None:
        params["limit"] = limit
    if source_priority_min is not None:
        params["source_priority_min"] = source_priority_min
    if lookback_hours is not None:
        params["lookback_hours"] = lookback_hours
    if source_category:
        params["source_category"] = source_category
    params["default_source_priority"] = 0

    sql = text(f"""
        SELECT
            a.article_id,
            a.title,
            a.summary,
            a.source_name,
            COALESCE(a.source_priority, s.source_priority, s.priority, :default_source_priority) AS source_priority,
            COALESCE(a.source_category, s.source_category, s.category) AS source_category,
            a.published_at,
            a.fetched_at,
            a.matched_keywords,
            a.related_tickers,
            a.classification_attempts
        FROM public.news_articles a
        LEFT JOIN public.news_sources s ON s.feed_url = a.feed_url
        WHERE a.classification_status = 'new'
        {article_clause}
        {source_priority_clause}
        {lookback_clause}
        {source_category_clause}
        ORDER BY COALESCE(a.source_priority, s.source_priority, s.priority, :default_source_priority) DESC NULLS LAST,
                 a.published_at DESC NULLS LAST,
                 a.fetched_at DESC NULLS LAST
    """)
    with engine.begin() as conn:
        rows = conn.execute(sql, params).mappings().all()
    articles = sort_classification_queue([dict(row) for row in rows], min_importance=min_importance)
    return articles[:limit] if limit is not None else articles


def reset_new_for_premium_sources(
    engine,
    *,
    source_priority_min: float,
    lookback_hours: float = 24,
    source_category: str | None = None,
) -> int:
    source_category_clause = "AND COALESCE(a.source_category, s.source_category, s.category) = :source_category" if source_category else ""
    params: dict[str, Any] = {
        "source_priority_min": source_priority_min,
        "lookback_hours": lookback_hours,
        "source_category": source_category,
        "default_source_priority": 0,
    }
    sql = text(f"""
        UPDATE public.news_articles a
        SET classification_status = 'new',
            last_classification_error = NULL
        FROM public.news_sources s
        WHERE s.feed_url = a.feed_url
          AND COALESCE(a.source_priority, s.source_priority, s.priority, :default_source_priority) >= :source_priority_min
          AND COALESCE(a.published_at, a.fetched_at) >= now() - (:lookback_hours * INTERVAL '1 hour')
          AND a.classification_status IN ('failed', 'skipped')
          {source_category_clause}
    """)
    with engine.begin() as conn:
        result = conn.execute(sql, params)
    return int(result.rowcount or 0)


def article_payload(article: dict) -> dict:
    return {
        "title": article.get("title"),
        "summary": article.get("summary"),
        "source_name": article.get("source_name"),
        "matched_keywords": article.get("matched_keywords") or [],
        "related_tickers": article.get("related_tickers") or [],
    }


def classification_prompt(article: dict) -> list[dict]:
    payload = article_payload(article)
    system = (
        "You are an investment news classifier. Return one valid JSON object only. "
        "No markdown. No prose. No explanation outside JSON. No trailing comments. "
        "All required fields must always be present. If unsure, use neutral/default values: "
        "sentiment_score 0, impact_score 0, confidence_score 0, themes [], "
        "event_type \"unknown\", time_horizon \"unknown\", affected_tickers [], reasoning \"\"."
    )
    user = {
        "task": "Classify this investment news item.",
        "article": payload,
        "required_json_schema": {
            "sentiment_score": "number from -1 to 1",
            "impact_score": "number from 0 to 100",
            "confidence_score": "number from 0 to 1",
            "themes": ["list", "of", "strings"],
            "event_type": "string",
            "time_horizon": "string",
            "affected_tickers": ["list", "of", "strings"],
            "reasoning": "string",
        },
        "required_fields": sorted(REQUIRED_FIELDS),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=True)},
    ]


def strip_deepseek_thinking(content: str) -> str:
    stripped = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL | re.IGNORECASE)
    return stripped.strip()


def _iter_json_object_candidates(content: str):
    in_string = False
    escape = False
    depth = 0
    start = None

    for idx, char in enumerate(content):
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield content[start:idx + 1]
                start = None


def extract_json_object(content: str) -> dict:
    cleaned = strip_deepseek_thinking(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        for candidate in _iter_json_object_candidates(cleaned):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        raise ClassificationError("Model response did not contain a valid JSON object")


def _require_number(value, field: str, low: float, high: float) -> float:
    if not isinstance(value, (int, float)):
        raise ClassificationError(f"{field} must be numeric")
    numeric = float(value)
    if numeric < low or numeric > high:
        raise ClassificationError(f"{field} must be between {low} and {high}")
    return numeric


def _require_string_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ClassificationError(f"{field} must be list[str]")
    return value


def validate_classification(payload: dict) -> dict:
    payload = {**DEFAULT_FIELDS, **payload}
    missing = sorted(REQUIRED_FIELDS - set(payload))
    if missing:
        raise ClassificationError(f"Missing required fields: {missing}")

    return {
        "sentiment_score": _require_number(payload["sentiment_score"], "sentiment_score", -1, 1),
        "impact_score": _require_number(payload["impact_score"], "impact_score", 0, 100),
        "confidence_score": _require_number(payload["confidence_score"], "confidence_score", 0, 1),
        "themes": _require_string_list(payload["themes"], "themes"),
        "event_type": str(payload["event_type"]),
        "time_horizon": str(payload["time_horizon"]),
        "affected_tickers": _require_string_list(payload["affected_tickers"], "affected_tickers"),
        "reasoning": str(payload["reasoning"]),
    }


def parse_model_response(content: str) -> dict:
    return validate_classification(extract_json_object(content))


def repair_prompt(raw_response: str) -> list[dict]:
    system = (
        "Convert the user content into one valid JSON object matching the schema. "
        "Return JSON only. No markdown. No prose. Required fields must be present. "
        "If a field is missing or unclear, use neutral/default values."
    )
    user = {
        "instruction": "Convert this into valid JSON matching the schema.",
        "raw_model_response": raw_response,
        "required_json_schema": {
            "sentiment_score": "number from -1 to 1",
            "impact_score": "number from 0 to 100",
            "confidence_score": "number from 0 to 1",
            "themes": ["list", "of", "strings"],
            "event_type": "string",
            "time_horizon": "string",
            "affected_tickers": ["list", "of", "strings"],
            "reasoning": "string",
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=True)},
    ]


def _ollama_chat(messages: list[dict], *, url: str, model: str, timeout: int) -> dict:
    response = requests.post(
        url,
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "format": "json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def classify_with_ollama(
    article: dict,
    *,
    url: str | None = None,
    model: str | None = None,
    timeout: int = 120,
    repair: bool = True,
) -> dict:
    selected_url = url or ollama_url()
    selected_model = model or ollama_model()
    body = _ollama_chat(
        classification_prompt(article),
        url=selected_url,
        model=selected_model,
        timeout=timeout,
    )
    content = body.get("message", {}).get("content", "")
    try:
        parsed = parse_model_response(content)
    except ClassificationError:
        if not repair:
            raise
        repair_body = _ollama_chat(
            repair_prompt(content),
            url=selected_url,
            model=selected_model,
            timeout=timeout,
        )
        repair_content = repair_body.get("message", {}).get("content", "")
        try:
            parsed = parse_model_response(repair_content)
        except ClassificationError as exc:
            raise ClassificationError(f"Repair failed: {exc}") from exc
        body = {"initial_response": body, "repair_response": repair_body}
    parsed["raw_response"] = body
    return parsed


def upsert_classification(engine, article_id: str, result: dict, *, provider: str = "ollama", model: str | None = None) -> None:
    sql = text("""
        INSERT INTO public.news_classifications (
            article_id, model_provider, model_name, classified_at,
            sentiment_score, impact_score, confidence_score, themes,
            event_type, time_horizon, affected_tickers, reasoning, raw_response
        )
        VALUES (
            :article_id, :model_provider, :model_name, :classified_at,
            :sentiment_score, :impact_score, :confidence_score, :themes,
            :event_type, :time_horizon, :affected_tickers, :reasoning,
            CAST(:raw_response AS jsonb)
        )
        ON CONFLICT (article_id)
        DO UPDATE SET
            model_provider = EXCLUDED.model_provider,
            model_name = EXCLUDED.model_name,
            classified_at = EXCLUDED.classified_at,
            sentiment_score = EXCLUDED.sentiment_score,
            impact_score = EXCLUDED.impact_score,
            confidence_score = EXCLUDED.confidence_score,
            themes = EXCLUDED.themes,
            event_type = EXCLUDED.event_type,
            time_horizon = EXCLUDED.time_horizon,
            affected_tickers = EXCLUDED.affected_tickers,
            reasoning = EXCLUDED.reasoning,
            raw_response = EXCLUDED.raw_response;
    """)
    status_sql = text("""
        UPDATE public.news_articles
        SET classification_status = 'analyzed',
            last_classification_error = NULL
        WHERE article_id = :article_id
    """)
    row = result.copy()
    row.update(
        {
            "article_id": article_id,
            "model_provider": provider,
            "model_name": model or ollama_model(),
            "classified_at": datetime.now(timezone.utc),
            "raw_response": json.dumps(result.get("raw_response") or {}, default=str),
        }
    )
    with engine.begin() as conn:
        conn.execute(sql, row)
        conn.execute(status_sql, {"article_id": article_id})


def record_classification_failure(engine, article_id: str, error: str) -> None:
    sql = text("""
        UPDATE public.news_articles
        SET classification_attempts = classification_attempts + 1,
            last_classification_error = :error,
            classification_status = CASE
                WHEN classification_attempts + 1 >= 3 THEN 'failed'
                ELSE classification_status
            END
        WHERE article_id = :article_id
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"article_id": article_id, "error": error})


def persist_classification(engine, article: dict, result: dict, *, dry_run: bool = False, model: str | None = None) -> None:
    if dry_run:
        return
    upsert_classification(engine, str(article["article_id"]), result, model=model)
