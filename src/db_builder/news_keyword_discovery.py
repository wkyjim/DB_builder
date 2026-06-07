"""Candidate keyword discovery from stored article headlines."""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text


STOPWORDS = {
    "about", "after", "against", "ahead", "amid", "and", "are", "as", "at",
    "before", "but", "by", "for", "from", "how", "in", "into", "is", "its",
    "new", "news", "of", "on", "over", "the", "this", "to", "under", "with",
    "a", "an", "be", "can", "could", "did", "do", "does", "had", "has",
    "have", "he", "her", "his", "i", "if", "it", "may", "more", "most",
    "not", "our", "she", "their", "they", "was", "we", "will", "would",
    "first", "real", "test", "what", "why", "you", "your",
}
GENERIC_TERMS = {
    "company", "companies", "investor", "investors", "market", "markets",
    "month", "report", "reports", "said", "says", "shares", "should",
    "stock", "stocks", "think", "today", "underperforming", "week", "year",
    "faces", "gain", "gains", "loss", "losses",
}
GENERIC_SINGLE_MODIFIERS = {
    "better", "big", "broad", "casual", "coming", "difficult", "extra",
    "full", "good", "great", "high", "important", "likely", "low", "major",
    "missing", "next", "old", "open", "rising", "strong", "surge", "twice",
    "boosts", "fall", "falls", "fit", "levels", "looks", "shape", "slip",
    "slide", "swallows",
}
FINANCIAL_TERMS = {
    "ai", "ai infrastructure", "bitcoin", "brent", "cloud", "copper",
    "credit", "crude oil", "defense", "defense spending", "dow jones",
    "earnings", "earnings guidance",
    "ethereum", "fed", "federal reserve", "fomc", "futures", "gold", "gpu",
    "inflation", "ipo", "lng", "nato", "nuclear", "oil", "oil prices",
    "openai", "opec", "powell", "quantum computing", "rate cut", "rate hike",
    "rates", "reits", "revenue", "semiconductor", "semiconductors", "spacex", "treasury",
    "treasury yields", "uranium", "wti", "yield curve",
}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,}")


def fetch_articles_for_discovery(engine, limit: int | None = None) -> pd.DataFrame:
    limit_clause = "LIMIT :limit" if limit is not None else ""
    params = {"limit": limit} if limit is not None else {}
    return pd.read_sql(
        text(f"""
            SELECT title, summary, matched_keywords, classification_status
            FROM public.news_articles
            WHERE title IS NOT NULL
            ORDER BY fetched_at DESC NULLS LAST
            {limit_clause}
        """),
        engine,
        params=params,
    )


def _clean_word(word: str) -> str:
    return word.strip("-").lower()


def _is_ticker_like(term: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,5}", term))


def _has_proper_noun_signal(term: str) -> bool:
    return any(part[:1].isupper() and not part.isupper() for part in term.split())


def _contains_financial_term(term: str) -> bool:
    lowered = term.lower()
    return any(financial in lowered for financial in FINANCIAL_TERMS)


def _is_clean_financial_phrase(normalized: str) -> bool:
    if normalized in FINANCIAL_TERMS:
        return True
    parts = normalized.split()
    if len(parts) == 2:
        return _contains_financial_term(normalized)
    return False


def is_quality_candidate(term: str) -> bool:
    normalized = " ".join(_clean_word(part) for part in term.split())
    if len(normalized) < 3:
        return normalized in FINANCIAL_TERMS
    parts = normalized.split()
    if any(part in STOPWORDS for part in parts):
        return False
    if normalized in GENERIC_TERMS:
        return False
    if len(parts) > 1 and any(
        part in GENERIC_TERMS or part in GENERIC_SINGLE_MODIFIERS
        for part in parts
    ):
        return False
    if len(parts) == 1 and normalized in GENERIC_SINGLE_MODIFIERS:
        return False
    if len(parts) == 1:
        return (
            normalized in FINANCIAL_TERMS
            or _is_ticker_like(term)
            or _has_proper_noun_signal(term)
        )
    return _is_clean_financial_phrase(normalized) or _has_proper_noun_signal(term)


def extract_candidate_terms(title: str) -> list[str]:
    raw_words = WORD_RE.findall(title or "")
    words = [w for w in raw_words if _clean_word(w) not in STOPWORDS]
    candidates = []
    for word in words:
        term = word if _is_ticker_like(word) else _clean_word(word)
        if is_quality_candidate(term):
            candidates.append(term.lower())
    for length in range(2, 5):
        for i in range(len(words) - length + 1):
            phrase_words = words[i:i + length]
            phrase = " ".join(_clean_word(word) for word in phrase_words)
            if is_quality_candidate(phrase):
                candidates.append(phrase)
    return candidates


def candidate_id(keyword: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.news_keyword_candidate:{keyword.lower()}"))


def candidate_quality_score(keyword: str, count: int) -> float:
    parts = keyword.split()
    score = min(0.35, count * 0.08)
    score += {1: 0.0, 2: 0.18, 3: 0.13, 4: 0.08}.get(len(parts), 0.0)
    if _contains_financial_term(keyword):
        score += 0.25
    if _is_ticker_like(keyword) or _has_proper_noun_signal(keyword):
        score += 0.15
    if len(parts) == 1 and keyword.lower() not in FINANCIAL_TERMS:
        score -= 0.10
    return round(max(0.0, min(0.98, score)), 4)


def generate_keyword_candidates(articles: list[dict] | pd.DataFrame, *, min_count: int = 2) -> list[dict]:
    records = articles.to_dict(orient="records") if isinstance(articles, pd.DataFrame) else articles
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)

    for article in records:
        matched = article.get("matched_keywords") or []
        if matched and len(matched) >= 2:
            continue
        title = article.get("title") or ""
        for term in set(extract_candidate_terms(title)):
            counts[term] += 1
            if len(examples[term]) < 5:
                examples[term].append(title)

    now = datetime.now(timezone.utc)
    candidates = []
    for keyword, count in counts.items():
        if count < min_count:
            continue
        if not is_quality_candidate(keyword):
            continue
        confidence = candidate_quality_score(keyword, count)
        auto_approve = confidence >= 0.90 and count >= 10
        candidates.append(
            {
                "candidate_id": candidate_id(keyword),
                "keyword": keyword,
                "theme": None,
                "subtheme": None,
                "source_headline_count": count,
                "example_headlines": examples[keyword],
                "confidence_score": round(confidence, 4),
                "first_seen_at": now,
                "last_seen_at": now,
                "status": "auto_approved" if auto_approve else "pending",
                "review_reason": "auto approval threshold met" if auto_approve else "needs human review",
            }
        )
    return sorted(
        candidates,
        key=lambda row: (-row["confidence_score"], -row["source_headline_count"], row["keyword"]),
    )
