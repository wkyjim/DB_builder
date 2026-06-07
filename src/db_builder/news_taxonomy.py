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
    "central bank policy": "Central Banks",
    "central banks": "Central Banks",
    "crypto regulation": "Crypto Regulation",
    "cryptocurrency market": "Crypto",
    "equity markets": "Market Sentiment",
    "stock market": "Market Sentiment",
    "stocks": "Market Sentiment",
    "investment performance": "Market Sentiment",
    "inflation expectations": "Inflation",
    "interest rates": "Interest Rates",
    "interest rate policy": "Interest Rates",
    "inflation": "Inflation",
    "treasury yields": "Rates",
    "treasury market": "Treasury Market",
    "federal reserve": "Fed",
    "fed": "Fed",
    "fed policy": "Monetary Policy",
    "fomc": "Monetary Policy",
    "monetary policy": "Monetary Policy",
    "financial stability": "Financial Stability",
    "etf regulation": "ETF Regulation",
    "regulation": "Regulation",
    "regulatory risk": "Regulation",
    "sec enforcement": "Regulation",
    "tariff risk": "Trade Policy",
    "trade policy": "Trade Policy",
    "trade war": "Trade Policy",
    "fiscal policy": "Fiscal Policy",
    "labor market": "Labor Market",
    "jobs report": "Labor Market",
    "housing market": "Housing",
    "housing": "Housing",
    "crude oil": "Oil",
    "crude oil prices": "Oil",
    "brent crude": "Oil",
    "oil market": "Oil",
    "oil prices": "Oil",
    "wti crude": "Oil",
    "natural gas": "Natural Gas",
    "energy security": "Energy Security",
    "defense spending": "Defense",
    "military spending": "Defense Spending",
    "national security": "National Security",
    "geo political": "Geopolitics",
    "geopolitics": "Geopolitics",
    "geopolitical risk": "Geopolitics",
    "iran risk": "Geopolitics",
    "middle east risk": "Geopolitics",
    "war risk": "Geopolitics",
    "nuclear energy": "Nuclear",
    "power grid": "Power Infrastructure",
    "electric grid": "Power Infrastructure",
    "banking sector": "Financials",
    "financial services": "Financials",
    "technology sector": "Technology",
    "semiconductor": "Semiconductors",
    "semiconductors": "Semiconductors",
    "cloud": "Cloud Computing",
    "cloud computing": "Cloud Computing",
    "gaming sector": "Consumer Discretionary",
    "retail sector": "Consumer Discretionary",
    "healthcare sector": "Healthcare",
    "energy sector": "Energy",
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


def normalize_theme_key(value: str) -> str:
    theme = str(value).strip().lower()
    theme = re.sub(r"[_]+", " ", theme)
    theme = re.sub(r"(?<=\w)-(?=\w)", " ", theme)
    theme = re.sub(r"\s+", " ", theme)
    return theme.strip()


def canonical_theme(value: str) -> str | None:
    theme = " ".join(str(value).strip().split())
    if not theme:
        return None
    return CANONICAL_THEME_MAP.get(normalize_theme_key(theme), theme)


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
