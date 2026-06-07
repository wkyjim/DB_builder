"""Article canonicalization and deduplication helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {"fbclid", "gclid"}
TRACKING_PREFIXES = ("utm_",)
WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    return WHITESPACE_RE.sub(" ", (value or "").strip())


def canonicalize_url(url: str | None) -> str:
    if not url:
        return ""

    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"

    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_PARAMS or any(key_lower.startswith(prefix) for prefix in TRACKING_PREFIXES):
            continue
        query_items.append((key, value))

    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def sha256_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def date_bucket(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date().isoformat() if value.tzinfo else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def title_fallback_hash(title: str | None, source_name: str | None, published_at=None) -> str:
    payload = "|".join(
        [
            normalize_text(title).lower(),
            normalize_text(source_name).lower(),
            date_bucket(published_at),
        ]
    )
    return sha256_hash(payload)


def article_hashes(url: str | None, title: str | None, source_name: str | None, published_at=None) -> tuple[str, str, str]:
    canonical_url = canonicalize_url(url)
    title_hash = title_fallback_hash(title, source_name, published_at)
    canonical_url_hash = sha256_hash(canonical_url) if canonical_url else title_hash
    return canonical_url, canonical_url_hash, title_hash
