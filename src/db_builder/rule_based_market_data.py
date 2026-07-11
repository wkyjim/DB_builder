"""SQL data access for deterministic market update reports."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db_builder.rule_based_config import CORE_MARKET_TICKERS, MACRO_SYMBOLS
from db_builder.positioning_flow_signals import fetch_positioning_flow_dashboard
from db_builder.etf_flow.repository import fetch_latest_analytics_output


ECONOMIC_SNAPSHOT_SERIES = [
    "PAYEMS", "UNRATE", "ICSA", "CCSA", "CIVPART",
    "GDPC1", "INDPRO", "RSAFS",
    "CPIAUCSL", "CPILFESL", "CPIAUCNS", "CPILFENS",
    "PCEPI", "PCEPILFE", "PPIFID", "PPIFES", "PPIACO",
    "DERIVED:CPI_HEADLINE_SA:MOM", "DERIVED:CPI_HEADLINE_SA:YOY",
    "DERIVED:CPI_CORE_SA:MOM", "DERIVED:CPI_CORE_SA:YOY",
    "DERIVED:CPI_HEADLINE_NSA:MOM", "DERIVED:CPI_HEADLINE_NSA:YOY",
    "DERIVED:CPI_CORE_NSA:MOM", "DERIVED:CPI_CORE_NSA:YOY",
    "DERIVED:PPI_HEADLINE:MOM", "DERIVED:PPI_HEADLINE:YOY",
    "DERIVED:PPI_CORE:MOM", "DERIVED:PPI_CORE:YOY",
    "DERIVED:PCE_HEADLINE:MOM", "DERIVED:PCE_HEADLINE:YOY",
    "DERIVED:PCE_CORE:MOM", "DERIVED:PCE_CORE:YOY",
    "DFF", "SOFR", "FEDFUNDS", "WALCL", "M1SL", "M2SL",
    "HOUST", "PERMIT", "MORTGAGE30US",
    "BAMLH0A0HYM2", "BAMLC0A0CM", "UMCSENT",
    "WB:CHN:NY.GDP.MKTP.KD.ZG", "WB:CHN:FP.CPI.TOTL.ZG", "WB:CHN:SL.UEM.TOTL.ZS",
    "WB:JPN:NY.GDP.MKTP.KD.ZG", "WB:JPN:FP.CPI.TOTL.ZG", "WB:JPN:SL.UEM.TOTL.ZS",
    "WB:DEU:NY.GDP.MKTP.KD.ZG", "WB:DEU:FP.CPI.TOTL.ZG", "WB:DEU:SL.UEM.TOTL.ZS",
    "WB:AUS:NY.GDP.MKTP.KD.ZG", "WB:AUS:FP.CPI.TOTL.ZG", "WB:AUS:SL.UEM.TOTL.ZS",
    "WB:EMU:NY.GDP.MKTP.KD.ZG", "WB:EMU:FP.CPI.TOTL.ZG", "WB:EMU:SL.UEM.TOTL.ZS",
    "ECB:EXR:D.USD.EUR.SP00.A", "ECB:EXR:D.JPY.EUR.SP00.A", "ECB:EXR:D.CNY.EUR.SP00.A", "ECB:EXR:D.AUD.EUR.SP00.A",
]


def _safe_read_sql(engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception:
        return pd.DataFrame()


def fetch_market_technicals(engine, *, tickers: list[str] | None = None) -> pd.DataFrame:
    selected = tickers or CORE_MARKET_TICKERS
    sql = """
        WITH latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker, name, date, close, pct_chg, volume, turnover, mkt_cap
            FROM public.us_equities
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        ),
        latest_indicators AS (
            SELECT DISTINCT ON (ticker)
                ticker, date AS indicator_date,
                ma_20, ma_50, ma_100, ma_200,
                rsi_14, macd, macd_signal, macd_hist,
                atr_14, volume_ma_20, volume_ratio_20,
                high_52w, low_52w,
                return_5d, return_20d, return_60d, volatility_20d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        )
        SELECT
            r.ticker, r.name, r.date, r.close, r.pct_chg, r.volume, r.turnover, r.mkt_cap,
            i.indicator_date, i.ma_20, i.ma_50, i.ma_100, i.ma_200,
            i.rsi_14, i.macd, i.macd_signal, i.macd_hist,
            i.atr_14, i.volume_ma_20, i.volume_ratio_20,
            i.high_52w, i.low_52w,
            i.return_5d, i.return_20d, i.return_60d, i.volatility_20d
        FROM latest_raw r
        LEFT JOIN latest_indicators i ON i.ticker = r.ticker
        ORDER BY r.ticker
    """
    return _safe_read_sql(engine, sql, {"tickers": selected})


def fetch_macro_snapshot(engine, *, symbols: list[str] | None = None) -> pd.DataFrame:
    selected = symbols or MACRO_SYMBOLS
    sql = """
        SELECT DISTINCT ON (symbol)
            symbol, name, asset_type, date, close, pct_chg, volume
        FROM public.macro
        WHERE symbol = ANY(:symbols)
        ORDER BY symbol, date DESC
    """
    return _safe_read_sql(engine, sql, {"symbols": selected})


def fetch_recent_news(engine, *, window_hours: int, limit: int = 80) -> pd.DataFrame:
    sql = """
        SELECT
            a.article_id,
            a.title,
            a.summary,
            a.source_name,
            a.source_type,
            a.source_priority,
            a.source_category,
            a.published_at,
            a.fetched_at,
            a.matched_keywords,
            a.related_tickers,
            c.sentiment_score,
            c.impact_score,
            c.confidence_score,
            c.themes,
            c.event_type,
            c.time_horizon,
            c.affected_tickers
        FROM public.news_articles a
        LEFT JOIN public.news_classifications c ON c.article_id = a.article_id
        WHERE COALESCE(a.published_at, a.fetched_at, c.classified_at)
            >= (now() - (:window_hours * INTERVAL '1 hour'))
        ORDER BY COALESCE(c.impact_score, 0) DESC,
                 COALESCE(a.source_priority, 50) DESC,
                 COALESCE(a.published_at, a.fetched_at) DESC
        LIMIT :limit
    """
    return _safe_read_sql(engine, sql, {"window_hours": window_hours, "limit": limit})


def fetch_news_signals(engine, *, window_hours: int, limit: int = 50) -> pd.DataFrame:
    sql = """
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.news_signals
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM public.news_signals s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY GREATEST(s.opportunity_score, s.risk_score) DESC,
                 s.article_count DESC
        LIMIT :limit
    """
    return _safe_read_sql(engine, sql, {"window_hours": window_hours, "limit": limit})


def fetch_economic_snapshot(engine, *, series_ids: list[str] | None = None) -> pd.DataFrame:
    selected = series_ids or ECONOMIC_SNAPSHOT_SERIES
    sql = """
        WITH latest_by_date AS (
            SELECT DISTINCT ON (series_id, date)
                date, series_id, series_name, source, country, region, category,
                frequency, value, unit, seasonal_adjustment, updated_at,
                realtime_start
            FROM public.economic_indicators
            WHERE series_id = ANY(:series_ids)
            ORDER BY series_id, date, realtime_start DESC
        ),
        ranked AS (
            SELECT
                date, series_id, series_name, source, country, region, category,
                frequency, value, unit, seasonal_adjustment, updated_at,
                ROW_NUMBER() OVER (
                    PARTITION BY series_id
                    ORDER BY date DESC
                ) AS rn
            FROM latest_by_date
        )
        SELECT *
        FROM ranked
        WHERE rn <= 2
        ORDER BY series_id, rn
    """
    return _safe_read_sql(engine, sql, {"series_ids": selected})


def fetch_sp500_constituent_technicals(engine) -> pd.DataFrame:
    sql = """
        WITH active_constituents AS (
            SELECT ticker, company_name, sector, industry
            FROM public.security_classification
            WHERE is_sp500 = true
              AND is_active = true
        ),
        latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker, name, date, close, pct_chg, volume, turnover, mkt_cap
            FROM public.us_equities
            WHERE ticker IN (SELECT ticker FROM active_constituents)
            ORDER BY ticker, date DESC
        ),
        latest_indicators AS (
            SELECT DISTINCT ON (ticker)
                ticker, date AS indicator_date,
                ma_20, ma_50, ma_100, ma_200,
                rsi_14, macd, macd_signal, macd_hist,
                atr_14, volume_ma_20, volume_ratio_20,
                high_52w, low_52w,
                return_5d, return_20d, return_60d, volatility_20d
            FROM public.us_equities_indicators
            WHERE ticker IN (SELECT ticker FROM active_constituents)
            ORDER BY ticker, date DESC
        )
        SELECT
            c.ticker,
            c.company_name,
            c.sector,
            c.industry,
            r.name,
            r.date,
            r.close,
            r.pct_chg,
            r.volume,
            r.turnover,
            r.mkt_cap,
            i.indicator_date,
            i.ma_20,
            i.ma_50,
            i.ma_100,
            i.ma_200,
            i.rsi_14,
            i.macd,
            i.macd_signal,
            i.macd_hist,
            i.atr_14,
            i.volume_ma_20,
            i.volume_ratio_20,
            i.high_52w,
            i.low_52w,
            i.return_5d,
            i.return_20d,
            i.return_60d,
            i.volatility_20d
        FROM active_constituents c
        LEFT JOIN latest_raw r ON r.ticker = c.ticker
        LEFT JOIN latest_indicators i ON i.ticker = c.ticker
        ORDER BY c.sector, c.ticker
    """
    return _safe_read_sql(engine, sql)


def collect_rule_based_inputs(engine, *, window_hours: int) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc),
        "window_hours": window_hours,
        "technicals": fetch_market_technicals(engine).to_dict(orient="records"),
        "sp500_technicals": fetch_sp500_constituent_technicals(engine).to_dict(orient="records"),
        "macro": fetch_macro_snapshot(engine).to_dict(orient="records"),
        "economic": fetch_economic_snapshot(engine).to_dict(orient="records"),
        "news": fetch_recent_news(engine, window_hours=window_hours).to_dict(orient="records"),
        "news_signals": fetch_news_signals(engine, window_hours=window_hours).to_dict(orient="records"),
        "positioning_flow": fetch_positioning_flow_dashboard(engine),
        "etf_flow_analytics": fetch_latest_analytics_output(engine),
    }
