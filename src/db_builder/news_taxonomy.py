"""Canonical taxonomy helpers for news signal dimensions."""

from __future__ import annotations

import re


CANONICAL_THEME_MAP = {
    "artificial intelligence": "AI",
    "ai": "AI",
    "stock market": "Market Sentiment",
    "investment performance": "Market Sentiment",
    "treasury yields": "Rates",
    "federal reserve": "Fed",
    "crude oil": "Oil",
    "defense spending": "Defense",
    "nuclear energy": "Nuclear",
}

GENERIC_REGION_TOKENS = {
    "U.S.",
    "US",
    "USA",
    "UNITED STATES",
    "EU",
    "UK",
    "CHINA",
    "JAPAN",
}

KNOWN_DOTTED_TICKERS = {
    "BRK.A",
    "BRK.B",
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,9}$")


def canonical_theme(value: str) -> str | None:
    theme = " ".join(str(value).strip().split())
    if not theme:
        return None
    return CANONICAL_THEME_MAP.get(theme.lower(), theme)


def normalize_ticker(value: str) -> str | None:
    ticker = " ".join(str(value).strip().split()).upper()
    if not ticker or ticker in GENERIC_REGION_TOKENS:
        return None
    if "." in ticker and ticker not in KNOWN_DOTTED_TICKERS:
        return None
    if ticker in KNOWN_DOTTED_TICKERS:
        return ticker
    if not TICKER_RE.fullmatch(ticker):
        return None
    return ticker
