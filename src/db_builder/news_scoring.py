"""Rule-based news scoring for deterministic market update reports."""

from __future__ import annotations

from datetime import datetime, timezone

from db_builder.market_strength import clamp, flt


NOISE_TERMS = {"should you buy", "best stocks", "market today", "things to know", "analyst says", "could be"}
SYSTEMIC_TERMS = {"fed", "fomc", "cpi", "ppi", "nfp", "payrolls", "tariff", "sanctions", "oil", "treasury", "vix"}


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def recency_weight(row: dict, *, now: datetime | None = None) -> float:
    timestamp = row.get("published_at") or row.get("fetched_at")
    if not timestamp:
        return 0.7
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return 0.7
    now = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    hours = max((now - timestamp).total_seconds() / 3600, 0)
    if hours <= 6:
        return 1.0
    if hours <= 24:
        return 0.85
    if hours <= 72:
        return 0.65
    return 0.45


def classify_news_relevance(row: dict) -> str:
    text = f"{row.get('title', '')} {row.get('summary', '')}".lower()
    themes = " ".join(str(item).lower() for item in _as_list(row.get("themes")))
    affected = _as_list(row.get("affected_tickers")) or _as_list(row.get("related_tickers"))
    if any(term in text for term in NOISE_TERMS):
        return "noisy"
    if any(term in text or term in themes for term in SYSTEMIC_TERMS):
        return "macro"
    if len(affected) == 1:
        return "single_name"
    if themes or affected:
        return "sector_theme"
    return "general"


def score_headline(row: dict, *, now: datetime | None = None) -> dict:
    source_weight = clamp(flt(row.get("source_priority"), 50) / 100, 0.2, 1.2)
    recency = recency_weight(row, now=now)
    impact = clamp(flt(row.get("impact_score"), 50) / 100, 0, 1)
    confidence = clamp(flt(row.get("confidence_score"), 0.5), 0, 1)
    sentiment = flt(row.get("sentiment_score"), 0)
    relevance = classify_news_relevance(row)
    relevance_weight = {
        "macro": 1.0,
        "sector_theme": 0.85,
        "single_name": 0.65,
        "general": 0.50,
        "noisy": 0.25,
    }[relevance]
    score = round(100 * source_weight * recency * impact * confidence * relevance_weight, 4)
    if sentiment > 0.15:
        sentiment_label = "positive"
    elif sentiment < -0.15:
        sentiment_label = "negative"
    else:
        sentiment_label = "neutral"
    return {
        **row,
        "news_score": clamp(score),
        "source_quality_weight": round(source_weight, 4),
        "recency_weight": round(recency, 4),
        "relevance_type": relevance,
        "sentiment_label": sentiment_label,
        "market_price_confirmed": False,
        "volume_confirmed": False,
        "persistence_score": round(clamp(score * 0.6 + impact * 40), 4),
    }


def score_news(rows: list[dict]) -> dict:
    scored = sorted([score_headline(row) for row in rows], key=lambda row: row["news_score"], reverse=True)
    confirmed = [row for row in scored if row["market_price_confirmed"] or row["volume_confirmed"]]
    noisy = [row for row in scored if row["relevance_type"] == "noisy"]
    positive = sum(row["sentiment_label"] == "positive" for row in scored)
    negative = sum(row["sentiment_label"] == "negative" for row in scored)
    total = max(len(scored), 1)
    confirmation_score = clamp(50 + ((positive - negative) / total) * 50)
    return {
        "score": round(confirmation_score, 4),
        "top_headlines": scored[:12],
        "price_confirmed": confirmed[:8],
        "noisy_headlines": noisy[:8],
        "sentiment_counts": {"positive": positive, "negative": negative, "neutral": total - positive - negative},
    }
