"""Long-term secular theme scoring for local investment intelligence."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db_builder.sector_intelligence import _as_list, _clamp, _float


SECULAR_THEME_TABLE = "public.secular_theme_signals"
ALL_WINDOW_HOURS = [168, 720, 2160]


@dataclass(frozen=True)
class SecularThemeDefinition:
    theme_name: str
    parent_theme: str
    related_etfs: tuple[str, ...]
    keywords: tuple[str, ...]
    related_tickers: tuple[str, ...] = ()


SECULAR_THEMES = [
    SecularThemeDefinition(
        "AI Infrastructure",
        "AI & Compute",
        ("QQQ", "XLK", "SMH", "SOXX"),
        ("AI infrastructure", "data center", "GPU", "inference", "training cluster", "AI capex"),
    ),
    SecularThemeDefinition(
        "Advanced AI Compute",
        "Semiconductors",
        ("SMH", "SOXX"),
        ("HBM", "HBM4", "advanced packaging", "CoWoS", "EUV", "AI accelerator", "photonics"),
    ),
    SecularThemeDefinition(
        "Cybersecurity",
        "Security",
        ("CIBR",),
        ("cybersecurity", "zero trust", "cloud security", "identity security", "SASE", "breach"),
    ),
    SecularThemeDefinition(
        "Defense Spending",
        "Defense",
        ("XAR",),
        ("defense spending", "missile", "drone", "NATO", "military procurement", "air defense"),
    ),
    SecularThemeDefinition(
        "Space & Satellite",
        "Defense / Space",
        ("XAR",),
        ("satellite", "launch", "rocket", "space force", "space defense"),
    ),
    SecularThemeDefinition(
        "Grid Expansion",
        "Power Infrastructure",
        ("GRID", "UTES", "XLU"),
        ("electric grid", "transmission", "substation", "grid modernization", "grid congestion"),
    ),
    SecularThemeDefinition(
        "Power Demand",
        "Power Infrastructure",
        ("GRID", "UTES", "XLU"),
        ("electricity demand", "data center power", "load growth", "power shortage"),
    ),
    SecularThemeDefinition(
        "Nuclear",
        "Energy Security",
        ("NLR", "XLU", "UTES"),
        ("nuclear energy", "uranium", "SMR", "reactor", "small modular reactor"),
    ),
    SecularThemeDefinition(
        "Energy Security",
        "Energy",
        ("XLE",),
        ("energy security", "oil supply", "LNG", "natural gas", "strategic petroleum reserve"),
    ),
    SecularThemeDefinition(
        "Reshoring",
        "Industrial Policy",
        ("XLI", "XAR", "SMH"),
        ("reshoring", "onshoring", "supply chain security", "domestic manufacturing"),
    ),
    SecularThemeDefinition(
        "Robotics & Automation",
        "Industrials",
        ("XLI", "QQQ"),
        ("robotics", "automation", "humanoid robot", "industrial AI"),
    ),
    SecularThemeDefinition(
        "GLP-1",
        "Healthcare",
        ("XLV",),
        ("GLP-1", "obesity drug", "diabetes drug", "weight loss drug"),
    ),
    SecularThemeDefinition(
        "Crypto Infrastructure",
        "Crypto",
        ("BTC-USD", "ETH-USD"),
        ("tokenization", "stablecoin", "bitcoin ETF", "ethereum ETF", "DeFi"),
    ),
]

THEME_EVIDENCE_ALIASES = {
    "AI Infrastructure": ("AI", "artificial intelligence", "data centers", "Nvidia", "NVDA", "AI stocks"),
    "Advanced AI Compute": ("semiconductors", "chips", "GPU", "Nvidia", "TSMC", "chipmaker"),
    "Cybersecurity": ("cyber", "cyber attack", "hack", "hacking", "ransomware"),
    "Defense Spending": ("defense", "military", "geopolitics", "war", "weapons"),
    "Space & Satellite": ("SpaceX", "space", "satellites"),
    "Grid Expansion": ("power grid", "power infrastructure", "electricity", "utilities"),
    "Power Demand": ("power", "electricity", "data centers", "utilities"),
    "Nuclear": ("nuclear", "uranium", "reactors", "nuclear power"),
    "Energy Security": ("energy", "oil", "gas", "brent", "wti", "opec", "geopolitics"),
    "Reshoring": ("industrial policy", "manufacturing", "supply chain", "tariffs"),
    "Robotics & Automation": ("robot", "robots", "automation", "industrial automation"),
    "GLP-1": ("glp1", "weight loss", "obesity", "diabetes", "drug approval"),
    "Crypto Infrastructure": ("bitcoin", "ethereum", "crypto", "blockchain", "stablecoins"),
}


def setup_secular_theme_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {SECULAR_THEME_TABLE} (
        signal_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        theme_name text,
        parent_theme text,
        status text,
        related_etfs text[],
        related_tickers text[],
        mention_count_7d integer,
        mention_count_30d integer,
        mention_count_90d integer,
        growth_score numeric,
        acceleration_score numeric,
        news_score numeric,
        momentum_score numeric,
        breadth_score numeric,
        macro_tailwind_score numeric,
        secular_score numeric,
        tactical_score numeric,
        theme_phase text,
        confidence numeric,
        top_subthemes text[],
        top_article_ids uuid[],
        drivers jsonb,
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_secular_theme_signals_run_theme
    ON {SECULAR_THEME_TABLE} (run_time, window_hours, theme_name);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def normalize_text(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).lower()


def keyword_matches(text_value: str, keywords: tuple[str, ...]) -> list[str]:
    text_value = normalize_text(text_value)
    matches = []
    for keyword in keywords:
        pattern = r"\b" + re.escape(keyword.lower()).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, text_value):
            matches.append(keyword)
    return matches


def theme_evidence_terms(theme: SecularThemeDefinition) -> tuple[str, ...]:
    terms = list(theme.keywords)
    terms.extend(THEME_EVIDENCE_ALIASES.get(theme.theme_name, ()))
    terms.append(theme.theme_name)
    terms.append(theme.parent_theme)
    seen = set()
    unique_terms = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique_terms.append(term)
    return tuple(unique_terms)


def _field_text(row: dict, field: str) -> str:
    value = row.get(field)
    if isinstance(value, list):
        return " ".join(map(str, value))
    return " ".join(map(str, _as_list(value))) if hasattr(value, "tolist") else normalize_text(value)


def article_theme_evidence(row: dict, theme: SecularThemeDefinition) -> tuple[float, list[str]]:
    terms = theme_evidence_terms(theme)
    title_summary_text = " ".join([normalize_text(row.get("title")), normalize_text(row.get("summary"))])
    metadata_text = " ".join([_field_text(row, "matched_keywords"), _field_text(row, "related_tickers")])
    classification_text = " ".join([_field_text(row, "themes"), _field_text(row, "affected_tickers")])
    title_summary_hits = keyword_matches(title_summary_text, terms)
    metadata_hits = keyword_matches(metadata_text, terms)
    classification_hits = keyword_matches(classification_text, terms)
    score = (len(title_summary_hits) * 3.0) + (len(metadata_hits) * 2.0) + (len(classification_hits) * 2.5)
    unique_hits = []
    for hit in title_summary_hits + metadata_hits + classification_hits:
        if hit not in unique_hits:
            unique_hits.append(hit)
    return score, unique_hits


def theme_to_etfs(theme_name: str) -> list[str]:
    for theme in SECULAR_THEMES:
        if theme.theme_name == theme_name:
            return list(theme.related_etfs)
    return []


def all_theme_etfs() -> list[str]:
    tickers = set()
    for theme in SECULAR_THEMES:
        tickers.update(theme.related_etfs)
    return sorted(tickers)


def fetch_theme_articles(engine) -> pd.DataFrame:
    sql = text("""
        SELECT
            a.article_id,
            a.title,
            a.summary,
            a.published_at,
            a.fetched_at,
            a.matched_keywords,
            a.related_tickers,
            c.sentiment_score,
            c.impact_score,
            c.confidence_score,
            c.themes,
            c.affected_tickers
        FROM public.news_articles a
        LEFT JOIN public.news_classifications c ON c.article_id = a.article_id
        WHERE COALESCE(a.published_at, a.fetched_at, c.classified_at) >= (now() - INTERVAL '90 days')
    """)
    return pd.read_sql(sql, engine)


def fetch_theme_news_signals(engine, *, window_hours: int) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.news_signals
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.news_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours})
    except Exception:
        return pd.DataFrame()


def fetch_theme_market_data(engine, tickers: list[str] | None = None) -> pd.DataFrame:
    selected = tickers or all_theme_etfs()
    sql = text("""
        WITH latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker, date, close, pct_chg
            FROM public.us_equities
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        ),
        latest_macro AS (
            SELECT DISTINCT ON (symbol)
                symbol AS ticker, date, close, pct_chg
            FROM public.macro
            WHERE symbol = ANY(:tickers)
            ORDER BY symbol, date DESC
        ),
        latest_prices AS (
            SELECT * FROM latest_raw
            UNION ALL
            SELECT *
            FROM latest_macro m
            WHERE NOT EXISTS (
                SELECT 1 FROM latest_raw r WHERE r.ticker = m.ticker
            )
        ),
        latest_indicators AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                date AS indicator_date,
                ma_50, ma_200, rsi_14,
                return_20d, return_60d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        )
        SELECT
            p.ticker, p.date, p.close, p.pct_chg,
            i.indicator_date, i.ma_50, i.ma_200, i.rsi_14,
            i.return_20d, i.return_60d
        FROM latest_prices p
        LEFT JOIN latest_indicators i ON i.ticker = p.ticker
    """)
    return pd.read_sql(sql, engine, params={"tickers": selected})


def _article_time(row: dict) -> pd.Timestamp | None:
    value = row.get("published_at") or row.get("fetched_at")
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value).tz_localize("UTC")


def matching_articles(theme: SecularThemeDefinition, articles_df: pd.DataFrame, *, run_time: datetime) -> list[dict]:
    matches = []
    for row in articles_df.to_dict(orient="records"):
        evidence, found = article_theme_evidence(row, theme)
        if evidence > 0:
            item = row.copy()
            item["_matched_subthemes"] = found
            item["_evidence_score"] = evidence
            item["_event_time"] = _article_time(row) or pd.Timestamp(run_time)
            matches.append(item)
    return matches


def mention_counts(matches: list[dict], *, run_time: datetime) -> tuple[int, int, int]:
    now = pd.Timestamp(run_time)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    counts = []
    for days in [7, 30, 90]:
        cutoff = now - pd.Timedelta(days=days)
        counts.append(sum((row.get("_event_time") is not None) and row["_event_time"] >= cutoff for row in matches))
    return int(counts[0]), int(counts[1]), int(counts[2])


def growth_score(mention_count_30d: int, mention_count_90d: int) -> float:
    if mention_count_90d <= 0:
        return 50.0
    current_intensity = mention_count_30d / 30
    base_intensity = mention_count_90d / 90
    return round(_clamp(50 + ((current_intensity / max(base_intensity, 0.01)) - 1) * 25), 4)


def acceleration_score(mention_count_7d: int, mention_count_30d: int) -> float:
    if mention_count_30d <= 0:
        return 50.0
    current_intensity = mention_count_7d / 7
    base_intensity = mention_count_30d / 30
    return round(_clamp(50 + ((current_intensity / max(base_intensity, 0.01)) - 1) * 25), 4)


def news_signal_evidence(theme: SecularThemeDefinition, news_df: pd.DataFrame) -> tuple[float, list[dict]]:
    if news_df.empty:
        return 0.0, []
    rows = []
    score = 0.0
    terms = theme_evidence_terms(theme)
    for row in news_df.to_dict(orient="records"):
        haystack = f"{row.get('dimension_value', '')} {row.get('dimension_type', '')}"
        hits = keyword_matches(haystack, terms)
        if hits or str(row.get("dimension_value")) == theme.theme_name:
            strength = _float(row.get("article_count")) + (_float(row.get("opportunity_score")) / 20)
            score += strength
            item = row.copy()
            item["_matched_subthemes"] = hits or [str(row.get("dimension_value"))]
            rows.append(item)
    return round(score, 4), rows


def score_theme_news(theme: SecularThemeDefinition, matches: list[dict], news_df: pd.DataFrame) -> tuple[float, list[str], list[str], list[str]]:
    if not matches and news_df.empty:
        return 50.0, [], [], ["news data missing; neutral score"]
    article_scores = []
    article_ids = []
    subtheme_counts: dict[str, int] = {}
    for row in matches:
        sentiment = _float(row.get("sentiment_score"))
        impact = _float(row.get("impact_score"), 50)
        confidence = _float(row.get("confidence_score"), 0.5)
        article_scores.append(_clamp(50 + (sentiment * 35) + ((impact - 50) * 0.25) + ((confidence - 0.5) * 20)))
        if row.get("article_id"):
            article_ids.append(str(row.get("article_id")))
        for subtheme in row.get("_matched_subthemes", []):
            subtheme_counts[subtheme] = subtheme_counts.get(subtheme, 0) + 1

    signal_evidence, signal_rows = news_signal_evidence(theme, news_df)
    signal_scores = []
    for row in signal_rows:
        signal_scores.append(
            _clamp(
                50
                + (_float(row.get("weighted_sentiment_score")) * 35)
                + (_float(row.get("opportunity_score"), 50) - _float(row.get("risk_score"), 20)) * 0.2
            )
        )
        for subtheme in row.get("_matched_subthemes", []):
            subtheme_counts[str(subtheme)] = subtheme_counts.get(str(subtheme), 0) + 1
        for article_id in _as_list(row.get("top_article_ids")):
            article_ids.append(str(article_id))
    scores = article_scores + signal_scores
    score = round(sum(scores) / len(scores), 4) if scores else 50.0
    top_subthemes = [item[0] for item in sorted(subtheme_counts.items(), key=lambda item: item[1], reverse=True)[:5]]
    unique_article_ids = []
    for article_id in article_ids:
        if article_id not in unique_article_ids:
            unique_article_ids.append(article_id)
    return score, top_subthemes, unique_article_ids[:5], [f"matched_articles={len(matches)}", f"news_signal_evidence={signal_evidence}", f"news_score={score}"]


def evidence_score(matches: list[dict], news_df: pd.DataFrame, theme: SecularThemeDefinition) -> float:
    article_evidence = sum(_float(row.get("_evidence_score")) for row in matches)
    signal_evidence, _ = news_signal_evidence(theme, news_df)
    count_bonus = min(len(matches) * 1.5, 20)
    return round(_clamp(article_evidence + signal_evidence + count_bonus), 4)


def score_theme_market(theme: SecularThemeDefinition, market_df: pd.DataFrame) -> tuple[float, float, bool, list[str]]:
    rows_by_ticker = {str(row.get("ticker")).upper(): row for row in market_df.to_dict(orient="records") if row.get("ticker")}
    rows = [rows_by_ticker[ticker.upper()] for ticker in theme.related_etfs if ticker.upper() in rows_by_ticker]
    if not rows:
        return 50.0, 50.0, False, ["ETF data missing; momentum and breadth neutral"]
    momentum_scores = []
    above = 0
    valid_breadth = 0
    overbought = False
    for row in rows:
        momentum_scores.append(_clamp(50 + (_float(row.get("return_20d")) * 0.45) + (_float(row.get("return_60d")) * 0.30)))
        close = _float(row.get("close"))
        ma_50 = _float(row.get("ma_50"))
        ma_200 = _float(row.get("ma_200"))
        if close and ma_50 and ma_200:
            valid_breadth += 2
            above += close > ma_50
            above += close > ma_200
        if _float(row.get("rsi_14"), 50) > 75:
            overbought = True
    momentum = round(sum(momentum_scores) / len(momentum_scores), 4)
    breadth = round((above / valid_breadth) * 100, 4) if valid_breadth else 50.0
    return momentum, breadth, overbought, [f"ETF data used: {', '.join(theme.related_etfs)}"]


def macro_tailwind_score(theme: SecularThemeDefinition, matches: list[dict], news_score_value: float) -> tuple[float, list[str]]:
    text_blob = " ".join(
        " ".join(
            [
                normalize_text(row.get("title")),
                normalize_text(row.get("summary")),
                " ".join(map(str, _as_list(row.get("themes")))).lower(),
            ]
        )
        for row in matches
    )
    score = 50.0
    drivers = []
    tailwinds = {
        "Defense": ("geopolitics", "war", "missile", "drone", "nato"),
        "Energy Security": ("geopolitics", "oil", "lng", "natural gas", "supply"),
        "AI & Compute": ("ai", "data center", "gpu", "capex"),
        "Power Infrastructure": ("power demand", "data center", "electric grid", "transmission"),
        "Security": ("breach", "cyberattack", "regulation", "security"),
    }
    terms = tailwinds.get(theme.parent_theme, ())
    if any(term in text_blob for term in terms):
        score += 20
        drivers.append(f"macro narrative tailwind for {theme.parent_theme}")
    if news_score_value >= 65:
        score += 10
        drivers.append("positive theme news supports tailwind")
    elif news_score_value <= 35:
        score -= 10
        drivers.append("negative theme news reduces tailwind")
    return round(_clamp(score), 4), drivers or ["no specific macro tailwind detected"]


def secular_score(growth: float, acceleration: float, news: float, breadth: float, macro_tailwind: float) -> float:
    return round((0.30 * growth) + (0.25 * acceleration) + (0.20 * news) + (0.15 * breadth) + (0.10 * macro_tailwind), 4)


def tactical_score(momentum: float, news: float, breadth: float, macro_tailwind: float) -> float:
    return round((0.35 * momentum) + (0.25 * news) + (0.20 * breadth) + (0.20 * macro_tailwind), 4)


def classify_theme_phase(
    *,
    mention_count_90d: int,
    growth: float,
    acceleration: float,
    news: float,
    momentum: float,
    breadth: float,
    secular: float,
    tactical: float,
    overbought: bool,
) -> str:
    if news < 35 and momentum < 40 and breadth < 40:
        return "broken"
    if growth < 40 and news < 45 and momentum < 45:
        return "fading"
    if secular >= 65 and tactical < 45:
        return "correction"
    if mention_count_90d <= 5 and acceleration >= 65 and news >= 50:
        return "emerging"
    if secular >= 65 and tactical >= 60 and mention_count_90d >= 10:
        return "mid_bull"
    if secular >= 60 and tactical >= 55:
        return "early_bull"
    if acceleration < 45 and (overbought or momentum < 52) and mention_count_90d >= 10:
        return "late_bull"
    if mention_count_90d >= 15 and momentum >= 60 and breadth >= 55:
        return "mid_bull"
    if growth >= 55 and news >= 50 and momentum >= 50:
        return "early_bull"
    return "emerging" if acceleration >= 60 and news >= 50 else "fading"


def build_secular_theme_signals(
    articles_df: pd.DataFrame,
    news_df: pd.DataFrame,
    market_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> list[dict]:
    selected_run_time = run_time or datetime.now(timezone.utc)
    signals = []
    for theme in SECULAR_THEMES:
        matches = matching_articles(theme, articles_df, run_time=selected_run_time)
        count_7d, count_30d, count_90d = mention_counts(matches, run_time=selected_run_time)
        evidence = evidence_score(matches, news_df, theme)
        growth = growth_score(count_30d, count_90d)
        acceleration = acceleration_score(count_7d, count_30d)
        news, top_subthemes, top_article_ids, news_drivers = score_theme_news(theme, matches, news_df)
        momentum, breadth, overbought, market_drivers = score_theme_market(theme, market_df)
        macro_tailwind, macro_drivers = macro_tailwind_score(theme, matches, news)
        secular = secular_score(growth, acceleration, news, breadth, macro_tailwind)
        tactical = tactical_score(momentum, news, breadth, macro_tailwind)
        phase = classify_theme_phase(
            mention_count_90d=count_90d,
            growth=growth,
            acceleration=acceleration,
            news=news,
            momentum=momentum,
            breadth=breadth,
            secular=secular,
            tactical=tactical,
            overbought=overbought,
        )
        confidence = round(min(1.0, (count_90d / 20) * 0.45 + (evidence / 100) * 0.25 + abs(secular - 50) / 50 * 0.30), 4)
        key = f"{selected_run_time.isoformat()}|{window_hours}|{theme.theme_name}"
        signals.append(
            {
                "signal_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.secular_theme:{key}")),
                "run_time": selected_run_time,
                "window_hours": window_hours,
                "theme_name": theme.theme_name,
                "parent_theme": theme.parent_theme,
                "status": "active",
                "related_etfs": list(theme.related_etfs),
                "related_tickers": list(theme.related_tickers),
                "mention_count_7d": count_7d,
                "mention_count_30d": count_30d,
                "mention_count_90d": count_90d,
                "growth_score": growth,
                "acceleration_score": acceleration,
                "news_score": news,
                "momentum_score": momentum,
                "breadth_score": breadth,
                "macro_tailwind_score": macro_tailwind,
                "secular_score": secular,
                "tactical_score": tactical,
                "theme_phase": phase,
                "confidence": confidence,
                "top_subthemes": top_subthemes,
                "top_article_ids": top_article_ids,
                "evidence_score": evidence,
                "drivers": {
                    "news": news_drivers,
                    "market": market_drivers,
                    "macro": macro_drivers,
                    "counts": [f"7d={count_7d}", f"30d={count_30d}", f"90d={count_90d}", f"evidence={evidence}"],
                },
            }
        )
    return sorted(signals, key=lambda row: row["secular_score"], reverse=True)


def upsert_secular_theme_signals(engine, signals: list[dict]) -> None:
    if not signals:
        return
    sql = text(f"""
        INSERT INTO {SECULAR_THEME_TABLE} (
            signal_id, run_time, window_hours, theme_name, parent_theme, status,
            related_etfs, related_tickers, mention_count_7d, mention_count_30d,
            mention_count_90d, growth_score, acceleration_score, news_score,
            momentum_score, breadth_score, macro_tailwind_score, secular_score,
            tactical_score, theme_phase, confidence, top_subthemes, top_article_ids,
            drivers, created_at
        )
        VALUES (
            :signal_id, :run_time, :window_hours, :theme_name, :parent_theme, :status,
            CAST(:related_etfs AS text[]), CAST(:related_tickers AS text[]),
            :mention_count_7d, :mention_count_30d, :mention_count_90d,
            :growth_score, :acceleration_score, :news_score, :momentum_score,
            :breadth_score, :macro_tailwind_score, :secular_score, :tactical_score,
            :theme_phase, :confidence, CAST(:top_subthemes AS text[]),
            CAST(:top_article_ids AS uuid[]), CAST(:drivers AS jsonb), now()
        )
        ON CONFLICT (signal_id)
        DO UPDATE SET
            status = EXCLUDED.status,
            mention_count_7d = EXCLUDED.mention_count_7d,
            mention_count_30d = EXCLUDED.mention_count_30d,
            mention_count_90d = EXCLUDED.mention_count_90d,
            growth_score = EXCLUDED.growth_score,
            acceleration_score = EXCLUDED.acceleration_score,
            news_score = EXCLUDED.news_score,
            momentum_score = EXCLUDED.momentum_score,
            breadth_score = EXCLUDED.breadth_score,
            macro_tailwind_score = EXCLUDED.macro_tailwind_score,
            secular_score = EXCLUDED.secular_score,
            tactical_score = EXCLUDED.tactical_score,
            theme_phase = EXCLUDED.theme_phase,
            confidence = EXCLUDED.confidence,
            top_subthemes = EXCLUDED.top_subthemes,
            top_article_ids = EXCLUDED.top_article_ids,
            drivers = EXCLUDED.drivers,
            created_at = now();
    """)
    rows = []
    for signal in signals:
        row = signal.copy()
        row["drivers"] = json.dumps(row.get("drivers") or {}, default=str)
        rows.append(row)
    with engine.begin() as conn:
        conn.execute(sql, rows)


def generate_secular_theme_signals(engine, *, window_hours: int, dry_run: bool = True) -> list[dict]:
    run_time = datetime.now(timezone.utc)
    articles_df = fetch_theme_articles(engine)
    news_df = fetch_theme_news_signals(engine, window_hours=window_hours)
    market_df = fetch_theme_market_data(engine)
    signals = build_secular_theme_signals(
        articles_df,
        news_df,
        market_df,
        window_hours=window_hours,
        run_time=run_time,
    )
    if not dry_run:
        setup_secular_theme_schema(engine)
        upsert_secular_theme_signals(engine, signals)
    return signals
