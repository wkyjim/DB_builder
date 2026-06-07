"""Sector regime engine using ETF trend, momentum, relative strength, and news inputs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db_builder.sector_intelligence import SECTOR_ETF_MAP, _as_list, _clamp, _float


SECTOR_REGIME_TABLE = "public.sector_regimes"


def setup_sector_regime_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {SECTOR_REGIME_TABLE} (
        signal_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        sector_name text,
        related_etfs text[],
        sector_regime text,
        cycle_phase text,
        confidence numeric,
        trend_score numeric,
        momentum_score numeric,
        relative_strength_score numeric,
        breadth_score numeric,
        news_score numeric,
        risk_score numeric,
        final_score numeric,
        trend_state text,
        momentum_state text,
        relative_strength_state text,
        cycle_state text,
        top_themes text[],
        top_article_ids uuid[],
        drivers jsonb,
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_regimes_run_sector
    ON {SECTOR_REGIME_TABLE} (run_time, window_hours, sector_name);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def all_sector_etfs() -> list[str]:
    tickers = {"SPY"}
    for etfs in SECTOR_ETF_MAP.values():
        tickers.update(etfs)
    return sorted(tickers)


def fetch_sector_market_data(engine, tickers: list[str] | None = None) -> pd.DataFrame:
    selected = tickers or all_sector_etfs()
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
                ma_20, ma_50, ma_200,
                rsi_14, macd, macd_signal, macd_hist,
                return_5d, return_20d, return_60d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        )
        SELECT
            p.ticker, p.date, p.close, p.pct_chg,
            i.indicator_date, i.ma_20, i.ma_50, i.ma_200,
            i.rsi_14, i.macd, i.macd_signal, i.macd_hist,
            i.return_5d, i.return_20d, i.return_60d
        FROM latest_prices p
        LEFT JOIN latest_indicators i ON i.ticker = p.ticker
    """)
    return pd.read_sql(sql, engine, params={"tickers": selected})


def fetch_sector_news_inputs(engine, *, window_hours: int) -> pd.DataFrame:
    sql = text("""
        WITH latest_sector AS (
            SELECT MAX(run_time) AS run_time
            FROM public.sector_signals
            WHERE window_hours = :window_hours
        ),
        latest_news AS (
            SELECT MAX(run_time) AS run_time
            FROM public.news_signals
            WHERE window_hours = :window_hours
        )
        SELECT
            'sector' AS source_table,
            sector_name AS dimension_value,
            article_count,
            weighted_sentiment_score,
            opportunity_score,
            risk_score,
            top_themes,
            top_article_ids
        FROM public.sector_signals s
        JOIN latest_sector r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        UNION ALL
        SELECT
            'theme' AS source_table,
            dimension_value,
            article_count,
            weighted_sentiment_score,
            opportunity_score,
            risk_score,
            ARRAY[dimension_value]::text[] AS top_themes,
            top_article_ids
        FROM public.news_signals n
        JOIN latest_news r ON r.run_time = n.run_time
        WHERE n.window_hours = :window_hours
          AND n.dimension_type = 'theme'
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours})
    except Exception:
        return pd.DataFrame()


def score_sector_trend(etf_rows: list[dict]) -> tuple[float, str, list[str]]:
    if not etf_rows:
        return 50.0, "neutral", ["ETF market data missing; neutral trend"]
    scores = []
    drivers = []
    for row in etf_rows:
        close = _float(row.get("close"))
        score = 50.0
        if close:
            for column, weight in [("ma_20", 8), ("ma_50", 12), ("ma_200", 15)]:
                ma = _float(row.get(column))
                if ma:
                    score += weight if close > ma else -weight
        macd_hist = _float(row.get("macd_hist"))
        if macd_hist > 0:
            score += 8
        elif macd_hist < 0:
            score -= 8
        scores.append(_clamp(score))
        drivers.append(f"{row.get('ticker')} trend={round(_clamp(score), 2)}")
    score = round(sum(scores) / len(scores), 4)
    state = "strong_uptrend" if score >= 75 else "uptrend" if score >= 60 else "neutral" if score >= 45 else "downtrend" if score >= 30 else "strong_downtrend"
    return score, state, drivers


def score_sector_momentum(etf_rows: list[dict]) -> tuple[float, str, list[str]]:
    if not etf_rows:
        return 50.0, "stable_positive", ["ETF market data missing; neutral momentum"]
    avg_5d = sum(_float(row.get("return_5d")) for row in etf_rows) / len(etf_rows)
    avg_20d = sum(_float(row.get("return_20d")) for row in etf_rows) / len(etf_rows)
    avg_60d = sum(_float(row.get("return_60d")) for row in etf_rows) / len(etf_rows)
    score = round(_clamp(50 + (avg_5d * 0.3) + (avg_20d * 0.4) + (avg_60d * 0.3)), 4)
    if avg_5d > avg_20d > avg_60d and avg_60d > 0:
        state = "accelerating"
    elif avg_20d > 0 and avg_60d > 0:
        state = "stable_positive"
    elif avg_5d < 0 and avg_20d > 0:
        state = "fading"
    elif avg_5d > 0 and avg_20d < -8:
        state = "oversold_rebound"
    else:
        state = "negative" if avg_5d < 0 or avg_20d < 0 else "stable_positive"
    drivers = [f"avg_5d={round(avg_5d, 2)}", f"avg_20d={round(avg_20d, 2)}", f"avg_60d={round(avg_60d, 2)}"]
    return score, state, drivers


def score_relative_strength(etf_rows: list[dict], spy_row: dict | None) -> tuple[float, str, list[str]]:
    if not etf_rows or not spy_row:
        return 50.0, "neutral", ["relative strength data missing; neutral score"]
    spy_5d = _float(spy_row.get("return_5d"))
    spy_20d = _float(spy_row.get("return_20d"))
    spy_60d = _float(spy_row.get("return_60d"))
    relative_scores = []
    drivers = []
    for row in etf_rows:
        rel = (
            (_float(row.get("return_5d")) - spy_5d) * 0.25
            + (_float(row.get("return_20d")) - spy_20d) * 0.45
            + (_float(row.get("return_60d")) - spy_60d) * 0.30
        )
        relative_scores.append(_clamp(50 + rel))
        drivers.append(f"{row.get('ticker')}-SPY relative={round(rel, 2)}")
    score = round(sum(relative_scores) / len(relative_scores), 4)
    state = "outperforming" if score >= 60 else "neutral" if score >= 45 else "underperforming"
    return score, state, drivers


def score_sector_breadth(etf_rows: list[dict]) -> float:
    if not etf_rows:
        return 50.0
    above_50 = 0
    above_200 = 0
    valid = 0
    for row in etf_rows:
        close = _float(row.get("close"))
        ma_50 = _float(row.get("ma_50"))
        ma_200 = _float(row.get("ma_200"))
        if close and ma_50 and ma_200:
            valid += 1
            above_50 += close > ma_50
            above_200 += close > ma_200
    if not valid:
        return 50.0
    return round(((above_50 / valid) * 50) + ((above_200 / valid) * 50), 4)


def score_sector_news(sector_name: str, news_rows: list[dict]) -> tuple[float, float, list[str], list[str], list[str]]:
    rows = [row for row in news_rows if str(row.get("dimension_value")) == sector_name]
    if not rows:
        return 50.0, 20.0, [], [], ["no direct sector news; neutral news score"]
    opportunity = sum(_float(row.get("opportunity_score")) for row in rows) / len(rows)
    risk = sum(_float(row.get("risk_score")) for row in rows) / len(rows)
    themes = []
    article_ids = []
    for row in rows:
        for theme in _as_list(row.get("top_themes")):
            if str(theme) not in themes:
                themes.append(str(theme))
        for article_id in _as_list(row.get("top_article_ids")):
            if str(article_id) not in article_ids:
                article_ids.append(str(article_id))
    drivers = [f"news opportunity={round(opportunity, 2)}", f"news risk={round(risk, 2)}"]
    return round(opportunity, 4), round(risk, 4), themes[:5], article_ids[:5], drivers


def score_sector_risk(etf_rows: list[dict], news_risk: float) -> tuple[float, list[str]]:
    score = news_risk * 0.5
    drivers = [f"news_risk={round(news_risk, 2)}"]
    if not etf_rows:
        return round(_clamp(score + 25), 4), drivers + ["missing ETF data adds neutral risk buffer"]
    penalties = []
    for row in etf_rows:
        ticker = row.get("ticker")
        close = _float(row.get("close"))
        rsi = _float(row.get("rsi_14"), 50)
        if rsi > 75:
            penalties.append(15)
            drivers.append(f"{ticker} RSI overbought={round(rsi, 2)}")
        if close and _float(row.get("ma_50")) and close < _float(row.get("ma_50")):
            penalties.append(8)
            drivers.append(f"{ticker} below ma_50")
        if close and _float(row.get("ma_200")) and close < _float(row.get("ma_200")):
            penalties.append(15)
            drivers.append(f"{ticker} below ma_200")
    score += sum(penalties) / max(len(etf_rows), 1)
    return round(_clamp(score), 4), drivers


def classify_sector_regime(final_score: float) -> str:
    if final_score >= 75:
        return "strong_bull"
    if final_score >= 60:
        return "bull"
    if final_score <= 25:
        return "strong_bear"
    if final_score <= 40:
        return "bear"
    return "neutral"


def classify_cycle_phase(
    trend_state: str,
    momentum_state: str,
    relative_strength_state: str,
    risk_score: float,
    etf_rows: list[dict],
) -> str:
    avg_20d = sum(_float(row.get("return_20d")) for row in etf_rows) / max(len(etf_rows), 1)
    avg_60d = sum(_float(row.get("return_60d")) for row in etf_rows) / max(len(etf_rows), 1)
    avg_rsi = sum(_float(row.get("rsi_14"), 50) for row in etf_rows) / max(len(etf_rows), 1)
    if avg_rsi < 30 and avg_20d < -10:
        return "capitulation"
    if trend_state in {"strong_downtrend", "downtrend"} and avg_20d < 0 and avg_60d < 0:
        return "bear"
    if trend_state in {"downtrend", "neutral"} and momentum_state == "negative":
        return "emerging_bear"
    if trend_state in {"uptrend", "strong_uptrend"} and momentum_state == "fading":
        return "late_bull"
    if trend_state in {"strong_uptrend", "uptrend"} and avg_20d < 0:
        return "correction"
    if trend_state == "uptrend" and relative_strength_state == "outperforming" and momentum_state == "accelerating":
        return "emerging_bull"
    if trend_state in {"uptrend", "strong_uptrend"} and avg_rsi < 70 and risk_score < 55:
        return "early_bull"
    if trend_state in {"uptrend", "strong_uptrend"} and momentum_state == "stable_positive":
        return "mid_bull"
    return "range_bound"


def final_sector_regime_score(trend: float, momentum: float, relative: float, breadth: float, news: float, risk: float) -> float:
    return round((0.25 * trend) + (0.20 * momentum) + (0.25 * relative) + (0.10 * breadth) + (0.10 * news) - (0.10 * risk), 4)


def build_sector_regimes(
    market_df: pd.DataFrame,
    news_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> list[dict]:
    selected_run_time = run_time or datetime.now(timezone.utc)
    market_rows = {str(row.get("ticker")).upper(): row for row in market_df.to_dict(orient="records") if row.get("ticker")}
    spy_row = market_rows.get("SPY")
    news_rows = news_df.to_dict(orient="records") if not news_df.empty else []
    signals = []
    for sector_name, etfs in SECTOR_ETF_MAP.items():
        etf_rows = [market_rows[ticker] for ticker in etfs if ticker in market_rows]
        trend_score, trend_state, trend_drivers = score_sector_trend(etf_rows)
        momentum_score, momentum_state, momentum_drivers = score_sector_momentum(etf_rows)
        relative_score, relative_state, relative_drivers = score_relative_strength(etf_rows, spy_row)
        breadth_score = score_sector_breadth(etf_rows)
        news_score, news_risk, top_themes, top_article_ids, news_drivers = score_sector_news(sector_name, news_rows)
        risk_score, risk_drivers = score_sector_risk(etf_rows, news_risk)
        final_score = final_sector_regime_score(trend_score, momentum_score, relative_score, breadth_score, news_score, risk_score)
        regime = classify_sector_regime(final_score)
        cycle_phase = classify_cycle_phase(trend_state, momentum_state, relative_state, risk_score, etf_rows)
        confidence = round(abs(final_score - 50) / 50, 4)
        key = f"{selected_run_time.isoformat()}|{window_hours}|{sector_name}"
        signals.append(
            {
                "signal_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.sector_regime:{key}")),
                "run_time": selected_run_time,
                "window_hours": window_hours,
                "sector_name": sector_name,
                "related_etfs": etfs,
                "sector_regime": regime,
                "cycle_phase": cycle_phase,
                "confidence": confidence,
                "trend_score": trend_score,
                "momentum_score": momentum_score,
                "relative_strength_score": relative_score,
                "breadth_score": breadth_score,
                "news_score": news_score,
                "risk_score": risk_score,
                "final_score": final_score,
                "trend_state": trend_state,
                "momentum_state": momentum_state,
                "relative_strength_state": relative_state,
                "cycle_state": cycle_phase,
                "top_themes": top_themes,
                "top_article_ids": top_article_ids,
                "drivers": {
                    "trend": trend_drivers,
                    "momentum": momentum_drivers,
                    "relative_strength": relative_drivers,
                    "news": news_drivers,
                    "risk": risk_drivers,
                },
            }
        )
    return sorted(signals, key=lambda row: row["final_score"], reverse=True)


def upsert_sector_regimes(engine, signals: list[dict]) -> None:
    if not signals:
        return
    sql = text(f"""
        INSERT INTO {SECTOR_REGIME_TABLE} (
            signal_id, run_time, window_hours, sector_name, related_etfs,
            sector_regime, cycle_phase, confidence, trend_score, momentum_score,
            relative_strength_score, breadth_score, news_score, risk_score, final_score,
            trend_state, momentum_state, relative_strength_state, cycle_state,
            top_themes, top_article_ids, drivers, created_at
        )
        VALUES (
            :signal_id, :run_time, :window_hours, :sector_name, CAST(:related_etfs AS text[]),
            :sector_regime, :cycle_phase, :confidence, :trend_score, :momentum_score,
            :relative_strength_score, :breadth_score, :news_score, :risk_score, :final_score,
            :trend_state, :momentum_state, :relative_strength_state, :cycle_state,
            CAST(:top_themes AS text[]), CAST(:top_article_ids AS uuid[]), CAST(:drivers AS jsonb), now()
        )
        ON CONFLICT (signal_id)
        DO UPDATE SET
            sector_regime = EXCLUDED.sector_regime,
            cycle_phase = EXCLUDED.cycle_phase,
            confidence = EXCLUDED.confidence,
            trend_score = EXCLUDED.trend_score,
            momentum_score = EXCLUDED.momentum_score,
            relative_strength_score = EXCLUDED.relative_strength_score,
            breadth_score = EXCLUDED.breadth_score,
            news_score = EXCLUDED.news_score,
            risk_score = EXCLUDED.risk_score,
            final_score = EXCLUDED.final_score,
            trend_state = EXCLUDED.trend_state,
            momentum_state = EXCLUDED.momentum_state,
            relative_strength_state = EXCLUDED.relative_strength_state,
            cycle_state = EXCLUDED.cycle_state,
            top_themes = EXCLUDED.top_themes,
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


def generate_sector_regimes(engine, *, window_hours: int, dry_run: bool = True) -> list[dict]:
    run_time = datetime.now(timezone.utc)
    market_df = fetch_sector_market_data(engine)
    news_df = fetch_sector_news_inputs(engine, window_hours=window_hours)
    signals = build_sector_regimes(market_df, news_df, window_hours=window_hours, run_time=run_time)
    if not dry_run:
        setup_sector_regime_schema(engine)
        upsert_sector_regimes(engine, signals)
    return signals
