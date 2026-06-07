"""Canonical taxonomy helpers for news signal dimensions."""

from __future__ import annotations

import re


CANONICAL_THEME_MAP = {
    "artificial intelligence": "AI",
    "ai": "AI",
    "ai regulation": "AI Regulation",
    "bank capital": "Bank Regulation",
    "bank regulation": "Bank Regulation",
    "capital markets": "Capital Markets",
    "central bank": "Central Banks",
    "central banks": "Central Banks",
    "crypto regulation": "Crypto Regulation",
    "stock market": "Market Sentiment",
    "investment performance": "Market Sentiment",
    "interest rates": "Interest Rates",
    "inflation": "Inflation",
    "treasury yields": "Rates",
    "treasury market": "Treasury Market",
    "federal reserve": "Fed",
    "fed": "Fed",
    "fomc": "Monetary Policy",
    "monetary policy": "Monetary Policy",
    "financial stability": "Financial Stability",
    "etf regulation": "ETF Regulation",
    "trade policy": "Trade Policy",
    "fiscal policy": "Fiscal Policy",
    "labor market": "Labor Market",
    "jobs report": "Labor Market",
    "housing market": "Housing",
    "housing": "Housing",
    "crude oil": "Oil",
    "natural gas": "Natural Gas",
    "energy security": "Energy Security",
    "defense spending": "Defense",
    "military spending": "Defense Spending",
    "national security": "National Security",
    "geopolitics": "Geopolitics",
    "nuclear energy": "Nuclear",
    "power grid": "Power Infrastructure",
    "electric grid": "Power Infrastructure",
    "financial services": "Financials",
    "semiconductor": "Semiconductors",
    "semiconductors": "Semiconductors",
    "cloud": "Cloud Computing",
    "cloud computing": "Cloud Computing",
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
