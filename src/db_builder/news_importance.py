"""Deterministic news importance scoring for classification queue ordering."""

from __future__ import annotations

import re


IMPORTANT_TERMS: dict[str, list[str]] = {
    "economic_data": [
        "CPI",
        "PPI",
        "payrolls",
        "NFP",
        "jobs report",
        "unemployment",
        "jobless claims",
        "GDP",
        "PMI",
        "ISM",
        "retail sales",
        "consumer confidence",
        "inflation expectations",
    ],
    "central_banks": [
        "Fed",
        "Federal Reserve",
        "FOMC",
        "Powell",
        "Waller",
        "Bowman",
        "ECB",
        "Lagarde",
        "BOJ",
        "Ueda",
        "PBOC",
        "BOE",
        "rate cut",
        "rate hike",
        "monetary policy",
        "quantitative tightening",
        "liquidity",
    ],
    "geopolitics": [
        "Iran",
        "Israel",
        "Hezbollah",
        "Hamas",
        "Russia",
        "Ukraine",
        "Taiwan",
        "China",
        "South China Sea",
        "NATO",
        "sanctions",
        "missile",
        "drone",
        "war",
        "ceasefire",
        "Strait of Hormuz",
        "Red Sea",
    ],
    "fiscal_policy_trade": [
        "tariff",
        "tariffs",
        "trade restrictions",
        "export controls",
        "tax bill",
        "fiscal stimulus",
        "budget deficit",
        "debt ceiling",
        "government spending",
        "industrial policy",
    ],
    "regulation": [
        "SEC",
        "DOJ",
        "FTC",
        "antitrust",
        "lawsuit",
        "enforcement",
        "approval",
        "ban",
        "investigation",
        "crypto regulation",
        "bank regulation",
        "AI regulation",
    ],
    "sector_policy": [
        "semiconductor subsidies",
        "CHIPS Act",
        "AI infrastructure",
        "power grid",
        "nuclear approval",
        "uranium",
        "defense spending",
        "cybersecurity regulation",
        "energy policy",
    ],
    "earnings_company_events": [
        "earnings",
        "results",
        "revenue",
        "profit",
        "EPS",
        "guidance",
        "outlook",
        "forecast",
        "beat",
        "miss",
        "margin",
        "demand",
        "backlog",
    ],
    "corporate_actions": [
        "merger",
        "acquisition",
        "takeover",
        "buyout",
        "spin-off",
        "IPO",
        "secondary offering",
        "share buyback",
        "dividend cut",
    ],
    "credit_stress": [
        "default",
        "bankruptcy",
        "downgrade",
        "liquidity crisis",
        "bank failure",
        "credit spreads",
        "distressed debt",
    ],
    "commodities_energy_shock": [
        "oil",
        "Brent",
        "WTI",
        "OPEC",
        "natural gas",
        "LNG",
        "copper",
        "gold",
        "uranium",
        "inventories",
        "supply disruption",
    ],
}

GENERIC_TERMS = [
    "stocks rise",
    "stocks fall",
    "market today",
    "what to watch",
    "things to know",
    "analyst says",
    "could be",
    "should you buy",
    "best stocks",
    "underperforming",
    "watch these stocks",
]

CATEGORY_WEIGHTS = {
    "economic_data": 35,
    "central_banks": 35,
    "geopolitics": 34,
    "fiscal_policy_trade": 30,
    "regulation": 26,
    "sector_policy": 30,
    "earnings_company_events": 24,
    "corporate_actions": 30,
    "credit_stress": 34,
    "commodities_energy_shock": 28,
}

GENERIC_PENALTY = 25
MAX_SCORE = 100
DEFAULT_EVENT_PRIORITY = 50
DEFAULT_MARKET_RELEVANCE_MULTIPLIER = 1.0

EVENT_PRIORITY_RULES = [
    ("FOMC decision", 100, ["FOMC decision", "FOMC statement", "Fed rate decision", "Federal Reserve decision"]),
    ("CPI / NFP", 100, ["CPI", "NFP", "nonfarm payrolls", "payrolls", "jobs report"]),
    ("Tariff announcement", 95, ["tariff announcement", "new tariffs", "tariffs", "export controls"]),
    ("Military escalation", 95, ["military escalation", "missile strikes", "drone attack", "war escalates", "Iran war"]),
    ("Earnings guidance change", 95, ["raises guidance", "cuts guidance", "lowers guidance", "guidance change", "earnings outlook"]),
    ("Earnings release", 90, ["earnings", "results", "EPS", "revenue", "profit"]),
    ("M&A", 90, ["merger", "acquisition", "takeover", "buyout"]),
    ("Regulatory action", 90, ["SEC enforcement", "enforcement action", "DOJ", "FTC", "antitrust", "lawsuit", "approval"]),
    ("Analyst note", 40, ["analyst says", "analyst note", "price target", "upgraded", "downgraded"]),
    ("Generic market recap", 20, ["stocks rise", "stocks fall", "market today", "what to watch", "things to know"]),
]

MARKET_RELEVANCE_RULES = [
    (
        "Top macro / central bank event",
        1.50,
        [
            "FOMC",
            "CPI",
            "PPI",
            "NFP",
            "Fed speech",
            "Federal Reserve speech",
            "ECB decision",
            "BOJ decision",
            "rate decision",
        ],
    ),
    (
        "Policy / geopolitical shock",
        1.40,
        [
            "tariffs",
            "trade war",
            "military escalation",
            "sanctions",
            "oil supply shock",
            "supply disruption",
            "missile strikes",
        ],
    ),
    (
        "Corporate / regulatory event",
        1.25,
        [
            "earnings release",
            "earnings",
            "guidance change",
            "raises guidance",
            "cuts guidance",
            "merger",
            "acquisition",
            "regulatory action",
            "SEC enforcement",
        ],
    ),
    (
        "Generic market recap",
        0.50,
        [
            "stocks rise",
            "stocks fall",
            "market today",
            "what to watch",
            "things to know",
        ],
    ),
]


def _term_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term.lower())
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


IMPORTANT_PATTERNS = {
    category: [(term, _term_pattern(term)) for term in terms]
    for category, terms in IMPORTANT_TERMS.items()
}
GENERIC_PATTERNS = [(term, _term_pattern(term)) for term in GENERIC_TERMS]
EVENT_PRIORITY_PATTERNS = [
    (event_type, priority, [(term, _term_pattern(term)) for term in terms])
    for event_type, priority, terms in EVENT_PRIORITY_RULES
]
MARKET_RELEVANCE_PATTERNS = [
    (label, multiplier, [(term, _term_pattern(term)) for term in terms])
    for label, multiplier, terms in MARKET_RELEVANCE_RULES
]


def _score_text(text: str, *, multiplier: float, reason_prefix: str) -> tuple[float, list[str], set[str]]:
    score = 0
    reasons = []
    matched_categories = set()

    for category, patterns in IMPORTANT_PATTERNS.items():
        matches = [term for term, pattern in patterns if pattern.search(text)]
        if not matches:
            continue
        matched_categories.add(category)
        score += CATEGORY_WEIGHTS[category] * multiplier
        score += min(len(matches) - 1, 3) * 5 * multiplier
        reasons.append(f"{reason_prefix}{category}: {', '.join(matches[:4])}")

    for term, pattern in GENERIC_PATTERNS:
        if pattern.search(text):
            score -= GENERIC_PENALTY * multiplier
            reasons.append(f"{reason_prefix}generic: {term}")

    return score, reasons, matched_categories


def score_news_importance(title: str | None, summary: str | None = None) -> dict:
    title_score, title_reasons, title_categories = _score_text(
        (title or "").lower(),
        multiplier=1.0,
        reason_prefix="title ",
    )
    summary_score, summary_reasons, summary_categories = _score_text(
        (summary or "").lower(),
        multiplier=0.35,
        reason_prefix="summary ",
    )

    score = title_score + summary_score
    reasons = title_reasons + summary_reasons
    matched_categories = title_categories | summary_categories

    if len(matched_categories) >= 2:
        score += 10
        reasons.append("multiple important categories")

    normalized = max(0, min(MAX_SCORE, round(score, 2)))
    return {
        "importance_score": float(normalized),
        "importance_reasons": reasons,
    }


def score_event_priority(title: str | None, summary: str | None = None) -> dict:
    text = f"{title or ''} {summary or ''}".lower()
    matches = []
    for event_type, priority, patterns in EVENT_PRIORITY_PATTERNS:
        terms = [term for term, pattern in patterns if pattern.search(text)]
        if terms:
            matches.append((priority, event_type, terms))
    if not matches:
        return {
            "event_type_priority": float(DEFAULT_EVENT_PRIORITY),
            "event_priority_label": "Other",
            "event_priority_reasons": [],
        }

    priority, event_type, terms = max(matches, key=lambda match: match[0])
    return {
        "event_type_priority": float(priority),
        "event_priority_label": event_type,
        "event_priority_reasons": [f"{event_type}: {', '.join(terms[:4])}"],
    }


def source_priority_score(article: dict) -> float:
    try:
        priority = float(article.get("source_priority"))
    except (TypeError, ValueError):
        priority = 75.0
    return max(0.0, min(priority, 100.0))


def score_market_relevance_multiplier(title: str | None, summary: str | None = None) -> dict:
    text = f"{title or ''} {summary or ''}".lower()
    matches = []
    for label, multiplier, patterns in MARKET_RELEVANCE_PATTERNS:
        terms = [term for term, pattern in patterns if pattern.search(text)]
        if terms:
            matches.append((multiplier, label, terms))
    if not matches:
        return {
            "market_relevance_multiplier": DEFAULT_MARKET_RELEVANCE_MULTIPLIER,
            "market_relevance_label": "Standard",
            "market_relevance_reasons": [],
        }

    multiplier, label, terms = max(matches, key=lambda match: match[0])
    return {
        "market_relevance_multiplier": float(multiplier),
        "market_relevance_label": label,
        "market_relevance_reasons": [f"{label}: {', '.join(terms[:4])}"],
    }


def classification_priority(article: dict) -> float:
    importance = float(article.get("importance_score") or 0)
    event_priority = float(article.get("event_type_priority") or DEFAULT_EVENT_PRIORITY)
    source_priority = source_priority_score(article)
    market_relevance_multiplier = float(article.get("market_relevance_multiplier") or DEFAULT_MARKET_RELEVANCE_MULTIPLIER)
    return round(importance * (event_priority / 100.0) * (source_priority / 100.0) * market_relevance_multiplier, 4)


def annotate_article_importance(article: dict) -> dict:
    row = article.copy()
    row.update(score_news_importance(row.get("title"), row.get("summary")))
    row.update(score_event_priority(row.get("title"), row.get("summary")))
    row.update(score_market_relevance_multiplier(row.get("title"), row.get("summary")))
    row["classification_priority"] = classification_priority(row)
    return row


def sort_classification_queue(articles: list[dict], *, min_importance: float | None = None) -> list[dict]:
    annotated = [annotate_article_importance(article) for article in articles]
    if min_importance is not None:
        annotated = [article for article in annotated if article["importance_score"] >= min_importance]

    return sorted(
        annotated,
        key=lambda article: (
            article.get("classification_priority") or 0,
            article.get("importance_score") or 0,
            article.get("event_type_priority") or 0,
            article.get("source_priority") or 0,
            article.get("market_relevance_multiplier") or 0,
            article.get("published_at") is not None,
            article.get("published_at"),
            article.get("fetched_at") is not None,
            article.get("fetched_at"),
        ),
        reverse=True,
    )
