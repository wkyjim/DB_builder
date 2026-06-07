"""Aggregate news and market data into investable sector signals."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text


SECTOR_SIGNAL_TABLE = "public.sector_signals"

THEME_SECTOR_MAP = {
    "AI": ["Technology", "Semiconductors"],
    "Semiconductors": ["Semiconductors"],
    "Cybersecurity": ["Cybersecurity"],
    "Defense Spending": ["Defense"],
    "Defense": ["Defense"],
    "Geopolitics": ["Defense", "Energy"],
    "Oil": ["Energy"],
    "Natural Gas": ["Energy"],
    "Nuclear": ["Nuclear", "Utilities"],
    "Power Infrastructure": ["Utilities", "Grid Infrastructure"],
    "Utilities": ["Utilities"],
    "Financials": ["Financials"],
    "Bank Regulation": ["Financials"],
    "Healthcare": ["Healthcare"],
    "Consumer Discretionary": ["Consumer Discretionary"],
    "Consumer Staples": ["Consumer Staples"],
    "Real Estate": ["Real Estate"],
    "Crypto": ["Crypto"],
    "Cryptocurrency": ["Crypto"],
}

SECTOR_ETF_MAP = {
    "Technology": ["QQQ", "XLK"],
    "Semiconductors": ["SMH", "SOXX"],
    "Cybersecurity": ["CIBR"],
    "Defense": ["XAR"],
    "Energy": ["XLE"],
    "Nuclear": ["NLR"],
    "Utilities": ["XLU", "UTES"],
    "Grid Infrastructure": ["GRID"],
    "Financials": ["XLF"],
    "Healthcare": ["XLV"],
    "Consumer Discretionary": ["XLY"],
    "Consumer Staples": ["XLP"],
    "Real Estate": ["XLRE"],
    "Crypto": ["BTC-USD", "ETH-USD"],
}


def setup_sector_signal_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {SECTOR_SIGNAL_TABLE} (
        signal_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        sector_name text,
        related_etfs text[],
        article_count integer,
        avg_sentiment_score numeric,
        weighted_sentiment_score numeric,
        opportunity_score numeric,
        risk_score numeric,
        momentum_score numeric,
        trend_score numeric,
        final_score numeric,
        rank integer,
        top_themes text[],
        top_article_ids uuid[],
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_signals_run_sector
    ON {SECTOR_SIGNAL_TABLE} (run_time, window_hours, sector_name);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def themes_to_sectors(theme: str) -> list[str]:
    return THEME_SECTOR_MAP.get(str(theme).strip(), [])


def sector_to_etfs(sector: str) -> list[str]:
    return SECTOR_ETF_MAP.get(str(sector).strip(), [])


def all_sector_etfs() -> list[str]:
    tickers = set()
    for etfs in SECTOR_ETF_MAP.values():
        tickers.update(etfs)
    return sorted(tickers)


def fetch_latest_news_signals(engine, *, window_hours: int) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.news_signals
            WHERE window_hours = :window_hours
        )
        SELECT
            s.dimension_value,
            s.article_count,
            s.avg_sentiment_score,
            s.weighted_sentiment_score,
            s.opportunity_score,
            s.risk_score,
            s.top_article_ids
        FROM public.news_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
          AND s.dimension_type = 'theme'
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours})


def fetch_latest_etf_market_data(engine, tickers: list[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    params = {"tickers": tickers}
    sql = text("""
        WITH latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                date,
                close,
                pct_chg
            FROM public.us_equities
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        ),
        latest_macro AS (
            SELECT DISTINCT ON (symbol)
                symbol AS ticker,
                date,
                close,
                pct_chg
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
                ma_50,
                ma_200,
                rsi_14,
                return_5d,
                return_20d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        )
        SELECT
            p.ticker,
            p.date,
            p.close,
            p.pct_chg,
            i.indicator_date,
            i.ma_50,
            i.ma_200,
            i.rsi_14,
            i.return_5d,
            i.return_20d
        FROM latest_prices p
        LEFT JOIN latest_indicators i ON i.ticker = p.ticker
    """)
    return pd.read_sql(sql, engine, params=params)


def _float(value, default: float = 0.0) -> float:
    if value is None or pd.isna(value):
        return default
    return float(value)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_etf_momentum(row: dict) -> float:
    if not row:
        return 50.0
    if row.get("return_20d") is not None and not pd.isna(row.get("return_20d")):
        return round(_clamp(50 + _float(row.get("return_20d"))), 4)
    if row.get("pct_chg") is not None and not pd.isna(row.get("pct_chg")):
        return round(_clamp(50 + (_float(row.get("pct_chg")) * 2)), 4)
    return 50.0


def score_etf_trend(row: dict) -> float:
    if not row:
        return 50.0
    close = _float(row.get("close"))
    ma_50 = _float(row.get("ma_50"))
    ma_200 = _float(row.get("ma_200"))
    if not close or not ma_50 or not ma_200:
        return 50.0
    if close >= ma_50 and close >= ma_200:
        return 70.0
    if close >= ma_50:
        return 60.0
    if close < ma_50 and close < ma_200:
        return 35.0
    return 45.0


def sector_market_scores(sector: str, market_rows: dict[str, dict]) -> tuple[float, float, list[str]]:
    etfs = sector_to_etfs(sector)
    if not etfs:
        return 50.0, 50.0, ["no ETF mapping"]

    momentum_scores = []
    trend_scores = []
    missing = []
    for etf in etfs:
        row = market_rows.get(etf)
        if not row:
            missing.append(etf)
            momentum_scores.append(50.0)
            trend_scores.append(50.0)
            continue
        momentum_scores.append(score_etf_momentum(row))
        trend_scores.append(score_etf_trend(row))

    momentum = round(sum(momentum_scores) / len(momentum_scores), 4)
    trend = round(sum(trend_scores) / len(trend_scores), 4)
    reasons = []
    if missing:
        reasons.append(f"missing ETF data neutralized: {', '.join(missing)}")
    else:
        reasons.append(f"ETF data used: {', '.join(etfs)}")
    return momentum, trend, reasons


def final_sector_score(news_opportunity: float, momentum: float, trend: float, risk: float) -> float:
    return round((0.35 * news_opportunity) + (0.25 * momentum) + (0.20 * trend) - (0.20 * risk), 4)


def _signal_id(run_time: datetime, window_hours: int, sector_name: str) -> str:
    key = f"{run_time.isoformat()}|{window_hours}|{sector_name.lower()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.sector_signal:{key}"))


def build_sector_signals(
    news_df: pd.DataFrame,
    market_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> list[dict]:
    selected_run_time = run_time or datetime.now(timezone.utc)
    if news_df.empty:
        return []

    market_rows = {
        str(row["ticker"]).upper(): row
        for row in market_df.to_dict(orient="records")
        if row.get("ticker")
    }
    grouped: dict[str, list[dict]] = {}
    for row in news_df.to_dict(orient="records"):
        theme = str(row.get("dimension_value") or "").strip()
        for sector in themes_to_sectors(theme):
            grouped.setdefault(sector, []).append(row)

    signals = []
    for sector, rows in grouped.items():
        article_count = sum(int(_float(row.get("article_count"))) for row in rows)
        article_weight = max(article_count, 1)
        avg_sentiment = sum(_float(row.get("avg_sentiment_score")) for row in rows) / len(rows)
        weighted_sentiment = (
            sum(_float(row.get("weighted_sentiment_score")) * int(_float(row.get("article_count"), 1)) for row in rows)
            / article_weight
        )
        news_opportunity = sum(_float(row.get("opportunity_score")) for row in rows) / len(rows)
        risk = sum(_float(row.get("risk_score")) for row in rows) / len(rows)
        momentum, trend, reasons = sector_market_scores(sector, market_rows)
        final_score = final_sector_score(news_opportunity, momentum, trend, risk)
        top_themes = [
            str(row.get("dimension_value"))
            for row in sorted(rows, key=lambda r: _float(r.get("opportunity_score")), reverse=True)[:5]
        ]
        top_article_ids = []
        for row in sorted(rows, key=lambda r: _float(r.get("opportunity_score")), reverse=True):
            for article_id in _as_list(row.get("top_article_ids")):
                if str(article_id) not in top_article_ids:
                    top_article_ids.append(str(article_id))
                if len(top_article_ids) >= 5:
                    break
            if len(top_article_ids) >= 5:
                break

        signals.append(
            {
                "signal_id": _signal_id(selected_run_time, window_hours, sector),
                "run_time": selected_run_time,
                "window_hours": window_hours,
                "sector_name": sector,
                "related_etfs": sector_to_etfs(sector),
                "article_count": article_count,
                "avg_sentiment_score": round(avg_sentiment, 6),
                "weighted_sentiment_score": round(weighted_sentiment, 6),
                "opportunity_score": round(news_opportunity, 4),
                "risk_score": round(risk, 4),
                "momentum_score": momentum,
                "trend_score": trend,
                "final_score": final_score,
                "rank": 0,
                "top_themes": top_themes,
                "top_article_ids": top_article_ids,
                "reasons": reasons,
            }
        )

    ranked = sorted(signals, key=lambda row: row["final_score"], reverse=True)
    for rank, signal in enumerate(ranked, start=1):
        signal["rank"] = rank
    return ranked


def upsert_sector_signals(engine, signals: list[dict]) -> None:
    if not signals:
        return
    sql = text(f"""
        INSERT INTO {SECTOR_SIGNAL_TABLE} (
            signal_id, run_time, window_hours, sector_name, related_etfs,
            article_count, avg_sentiment_score, weighted_sentiment_score,
            opportunity_score, risk_score, momentum_score, trend_score,
            final_score, rank, top_themes, top_article_ids, created_at
        )
        VALUES (
            :signal_id, :run_time, :window_hours, :sector_name, CAST(:related_etfs AS text[]),
            :article_count, :avg_sentiment_score, :weighted_sentiment_score,
            :opportunity_score, :risk_score, :momentum_score, :trend_score,
            :final_score, :rank, CAST(:top_themes AS text[]), CAST(:top_article_ids AS uuid[]), now()
        )
        ON CONFLICT (signal_id)
        DO UPDATE SET
            related_etfs = EXCLUDED.related_etfs,
            article_count = EXCLUDED.article_count,
            avg_sentiment_score = EXCLUDED.avg_sentiment_score,
            weighted_sentiment_score = EXCLUDED.weighted_sentiment_score,
            opportunity_score = EXCLUDED.opportunity_score,
            risk_score = EXCLUDED.risk_score,
            momentum_score = EXCLUDED.momentum_score,
            trend_score = EXCLUDED.trend_score,
            final_score = EXCLUDED.final_score,
            rank = EXCLUDED.rank,
            top_themes = EXCLUDED.top_themes,
            top_article_ids = EXCLUDED.top_article_ids,
            created_at = now();
    """)
    rows = [{key: value for key, value in signal.items() if key != "reasons"} for signal in signals]
    with engine.begin() as conn:
        conn.execute(sql, rows)


def generate_sector_intelligence(engine, *, window_hours: int, dry_run: bool = True) -> list[dict]:
    run_time = datetime.now(timezone.utc)
    news_df = fetch_latest_news_signals(engine, window_hours=window_hours)
    market_df = fetch_latest_etf_market_data(engine, all_sector_etfs())
    signals = build_sector_signals(news_df, market_df, window_hours=window_hours, run_time=run_time)
    if not dry_run:
        setup_sector_signal_schema(engine)
        upsert_sector_signals(engine, signals)
    return signals
