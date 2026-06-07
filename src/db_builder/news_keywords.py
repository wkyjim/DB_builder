"""Seed investment news keyword coverage."""

from __future__ import annotations

import uuid


KEYWORD_SEEDS = {
    "Macro": ["Federal Reserve", "FOMC", "Powell", "ECB", "BOJ", "PBOC", "BOE", "rate cut", "rate hike", "inflation", "CPI", "PPI", "GDP", "PMI", "NFP", "unemployment"],
    "Monetary Policy": ["monetary policy", "Fed minutes", "FOMC statement", "dot plot", "quantitative tightening", "balance sheet runoff"],
    "Central Banks": ["central bank", "Federal Reserve", "ECB", "BOJ", "PBOC", "BOE", "SNB"],
    "Interest Rates": ["interest rates", "policy rate", "terminal rate", "rate path", "rate expectations"],
    "Inflation": ["inflation", "disinflation", "core CPI", "core PCE", "wage inflation"],
    "Rates": ["Treasury yields", "yield curve", "real yields", "duration", "term premium"],
    "Treasury Market": ["Treasury market", "Treasury auction", "Treasury refunding", "bill issuance", "bond supply"],
    "Credit": ["investment grade", "high yield", "credit spreads", "distressed debt", "private credit"],
    "Financial Stability": ["financial stability", "bank stress", "liquidity stress", "systemic risk", "deposit outflows"],
    "Bank Regulation": ["bank regulation", "Basel III", "capital requirements", "stress test", "bank supervision"],
    "Capital Markets": ["capital markets", "IPO market", "secondary offering", "market structure", "clearing rules"],
    "ETF Regulation": ["ETF regulation", "spot ETF", "ETF approval", "ETF filing"],
    "Crypto Regulation": ["crypto regulation", "SEC crypto", "stablecoin regulation", "digital asset regulation"],
    "AI Regulation": ["AI regulation", "AI safety", "AI rules", "AI oversight"],
    "Trade Policy": ["trade policy", "tariffs", "export controls", "import restrictions", "trade war"],
    "Fiscal Policy": ["fiscal policy", "budget deficit", "government spending", "tax policy", "debt ceiling"],
    "Labor Market": ["labor market", "jobless claims", "nonfarm payrolls", "wage growth", "unemployment"],
    "Housing": ["housing", "housing starts", "mortgage rates", "home sales", "homebuilder sentiment"],
    "FX": ["DXY", "EURUSD", "USDJPY", "USDCNH", "carry trade"],
    "Energy": ["WTI", "Brent", "OPEC", "natural gas", "LNG"],
    "Energy Security": ["energy security", "strategic petroleum reserve", "power reliability", "grid reliability"],
    "Metals": ["gold", "silver", "copper", "aluminum", "lithium", "rare earth"],
    "Technology": ["AI", "LLM", "GPU", "semiconductors", "cloud computing", "cybersecurity", "data center"],
    "Healthcare": ["biotech", "FDA", "drug approval"],
    "Industrials": ["automation", "robotics", "aerospace", "logistics"],
    "Consumer": ["e-commerce", "retail", "luxury", "travel"],
    "Real Estate": ["REIT", "commercial real estate", "housing"],
    "Utilities": ["electric grid", "transmission", "substation", "electricity demand"],
    "Nuclear": ["uranium", "nuclear energy", "SMR", "small modular reactor"],
    "Defense": ["defense spending", "NATO", "drone", "missile", "military procurement"],
    "Defense Spending": ["defense budget", "military spending", "weapons procurement", "defense appropriations"],
    "National Security": ["national security", "critical infrastructure", "cyber threat", "export ban"],
    "Space": ["satellite", "rocket", "launch services"],
    "Crypto": ["Bitcoin", "Ethereum", "stablecoin", "DeFi", "tokenization"],
    "Geopolitics": ["Iran", "Israel", "Hezbollah", "Hormuz", "Russia", "Ukraine", "Taiwan", "South China Sea", "sanctions"],
    "Fiscal": ["budget deficit", "government spending", "tax cuts", "tax hikes", "debt ceiling"],
}


def keyword_id(keyword: str, theme: str, subtheme: str | None = None) -> str:
    value = "|".join([theme.strip().lower(), (subtheme or "").strip().lower(), keyword.strip().lower()])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.news_keywords:{value}"))


def seed_keyword_records(default_priority: int = 100) -> list[dict]:
    records = []
    for theme, keywords in KEYWORD_SEEDS.items():
        for keyword in keywords:
            records.append(
                {
                    "keyword_id": keyword_id(keyword, theme),
                    "theme": theme,
                    "subtheme": None,
                    "keyword": keyword,
                    "priority": default_priority,
                    "enabled": True,
                }
            )
    return records


def enabled_keyword_lookup(records: list[dict] | None = None) -> dict[str, dict]:
    source = records if records is not None else seed_keyword_records()
    return {r["keyword"].lower(): r for r in source if r.get("enabled", True)}
