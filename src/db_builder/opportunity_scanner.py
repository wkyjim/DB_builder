"""Ticker opportunity scanning from local news signals and equity indicators."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text


OPPORTUNITY_TABLE = "public.opportunity_signals"

WATCHLIST_UNIVERSE = {
    "GRID", "UTES", "AIPO", "NLR", "XAR", "SMH", "SOXX", "CIBR", "TAN",
    "XLU", "XLE", "XLF", "XLV", "XLI", "XLY", "XLP", "XLC", "XLB", "XLRE",
    "SPY", "QQQ", "IWM", "BTC-USD", "ETH-USD",
}
ETF_UNIVERSE = WATCHLIST_UNIVERSE - {"BTC-USD", "ETH-USD"}
MEME_TICKERS = {"GME", "AMC", "BBBY", "KOSS", "HKD", "HOLO", "MULN"}
UNIVERSES = {
    "watchlist": WATCHLIST_UNIVERSE,
    "etf": ETF_UNIVERSE,
    "all": None,
}


def setup_opportunity_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {OPPORTUNITY_TABLE} (
        opportunity_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        ticker text,
        latest_date date,
        news_opportunity_score numeric,
        news_risk_score numeric,
        technical_score numeric,
        opportunity_score numeric,
        risk_score numeric,
        signal_label text,
        reasons text[],
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunity_signals_run_ticker
    ON {OPPORTUNITY_TABLE} (run_time, window_hours, ticker);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def fetch_ticker_news_signals(engine, *, window_hours: int, run_time: datetime | None = None) -> pd.DataFrame:
    selected_run_time = run_time or datetime.now(timezone.utc)
    sql = text("""
        SELECT
            dimension_value AS ticker,
            article_count,
            high_impact_count,
            weighted_sentiment_score,
            opportunity_score,
            risk_score
        FROM public.news_signals
        WHERE dimension_type = 'ticker'
          AND window_hours = :window_hours
          AND run_time >= (CAST(:run_time AS timestamptz) - (:window_hours * INTERVAL '1 hour'))
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "run_time": selected_run_time})


def fetch_latest_equity_indicators(engine, tickers: list[str] | None = None) -> pd.DataFrame:
    raw_filter = "WHERE ticker = ANY(:tickers)" if tickers else ""
    macro_filter = "WHERE symbol = ANY(:tickers)" if tickers else ""
    params = {"tickers": tickers} if tickers else {}
    sql = text(f"""
        WITH latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                date,
                close,
                pct_chg,
                mkt_cap
            FROM public.us_equities
            {raw_filter}
            ORDER BY ticker, date DESC
        ),
        latest_macro AS (
            SELECT DISTINCT ON (symbol)
                symbol AS ticker,
                date,
                close,
                pct_chg,
                NULL::numeric AS mkt_cap
            FROM public.macro
            {macro_filter}
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
                macd_hist,
                volume_ratio_20,
                return_5d,
                return_20d,
                volatility_20d
            FROM public.us_equities_indicators
            {raw_filter}
            ORDER BY ticker, date DESC
        )
        SELECT
            p.ticker,
            p.date,
            p.close,
            p.pct_chg,
            p.mkt_cap,
            i.indicator_date,
            i.ma_50,
            i.ma_200,
            i.rsi_14,
            i.macd_hist,
            i.volume_ratio_20,
            i.return_5d,
            i.return_20d,
            i.volatility_20d
        FROM latest_prices p
        LEFT JOIN latest_indicators i ON i.ticker = p.ticker
    """)
    return pd.read_sql(sql, engine, params=params)


def _float(value, default: float = 0.0) -> float:
    if pd.isna(value):
        return default
    return float(value)


def _has_value(value) -> bool:
    return value is not None and not pd.isna(value)


def has_indicator_data(row: dict) -> bool:
    return any(
        _has_value(row.get(column))
        for column in ["indicator_date", "rsi_14", "macd_hist", "return_5d", "return_20d"]
    )


def is_approved_watchlist_ticker(ticker: str) -> bool:
    return ticker.upper().strip() in WATCHLIST_UNIVERSE


def normalize_universe(universe: str) -> str:
    selected = str(universe or "watchlist").lower().strip()
    if selected not in UNIVERSES:
        raise ValueError(f"Unsupported universe: {universe}")
    return selected


def exclusion_reasons(
    ticker: str,
    indicator_row: dict,
    *,
    universe: str = "watchlist",
    include_meme: bool = False,
) -> list[str]:
    selected_universe = normalize_universe(universe)
    normalized_ticker = ticker.upper().strip()
    allowed_tickers = UNIVERSES[selected_universe]
    approved = is_approved_watchlist_ticker(normalized_ticker)
    reasons = []

    if normalized_ticker in MEME_TICKERS and not include_meme:
        reasons.append("excluded meme/high-noise ticker")
    if allowed_tickers is not None and normalized_ticker not in allowed_tickers:
        reasons.append(f"excluded by {selected_universe} universe")
    if not _has_value(indicator_row.get("close")):
        reasons.append("missing latest close")
    if not has_indicator_data(indicator_row) and not approved:
        reasons.append("missing indicator data for non-watchlist ticker")
    if not _has_value(indicator_row.get("mkt_cap")) and not approved:
        reasons.append("missing market cap for non-watchlist ticker")

    return reasons


def technical_score(row: dict) -> tuple[float, list[str]]:
    if not row:
        return 0.0, ["no local technical data"]

    if not has_indicator_data(row):
        return 0.0, ["no local technical indicators"]

    score = 0.0
    reasons = []
    close = _float(row.get("close"))
    ma_50 = _float(row.get("ma_50"))
    ma_200 = _float(row.get("ma_200"))
    rsi = _float(row.get("rsi_14"), 50.0)
    macd_hist = _float(row.get("macd_hist"))
    return_5d = _float(row.get("return_5d"))
    return_20d = _float(row.get("return_20d"))
    volume_ratio = _float(row.get("volume_ratio_20"), 1.0)

    if 45 <= rsi <= 65:
        score += 12
        reasons.append(f"balanced RSI {round(rsi, 2)}")
    elif 30 <= rsi < 45:
        score += 8
        reasons.append(f"recovering RSI {round(rsi, 2)}")
    elif rsi > 75:
        score -= 12
        reasons.append(f"overbought RSI {round(rsi, 2)}")
    elif rsi < 25:
        score -= 8
        reasons.append(f"weak RSI {round(rsi, 2)}")

    if macd_hist > 0:
        score += 10
        reasons.append("positive MACD histogram")
    elif macd_hist < 0:
        score -= 6
        reasons.append("negative MACD histogram")

    if return_5d > 0:
        score += min(return_5d, 12)
        reasons.append(f"positive 5d return {round(return_5d, 2)}")
    elif return_5d < -3:
        score -= min(abs(return_5d), 12)
        reasons.append(f"negative 5d return {round(return_5d, 2)}")

    if return_20d > 0:
        score += min(return_20d / 2, 10)
        reasons.append(f"positive 20d return {round(return_20d, 2)}")
    elif return_20d < -5:
        score -= min(abs(return_20d) / 2, 10)
        reasons.append(f"negative 20d return {round(return_20d, 2)}")

    if volume_ratio >= 1.5:
        score += 8
        reasons.append(f"elevated volume ratio {round(volume_ratio, 2)}")

    if close and ma_50 and close < ma_50:
        score -= 10
        reasons.append(f"close below ma_50 {round(ma_50, 2)}")
    if close and ma_200 and close < ma_200:
        score -= 8
        reasons.append(f"close below ma_200 {round(ma_200, 2)}")

    return round(score, 4), reasons[:6]


def classify_opportunity(opportunity_score: float, risk_score: float) -> str:
    spread = opportunity_score - risk_score
    if spread >= 25:
        return "opportunity"
    if spread <= -20:
        return "risk"
    return "watchlist"


def build_opportunity_signals(
    news_df: pd.DataFrame,
    indicators_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
    universe: str = "watchlist",
    include_meme: bool = False,
) -> list[dict]:
    selected_run_time = run_time or datetime.now(timezone.utc)
    selected_universe = normalize_universe(universe)
    if news_df.empty:
        return []

    indicators = {
        str(row["ticker"]).upper(): row
        for row in indicators_df.to_dict(orient="records")
        if row.get("ticker")
    }
    signals = []

    for row in news_df.to_dict(orient="records"):
        ticker = str(row.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        indicator_row = indicators.get(ticker, {})
        excluded = exclusion_reasons(
            ticker,
            indicator_row,
            universe=selected_universe,
            include_meme=include_meme,
        )
        if excluded:
            continue

        tech_score, tech_reasons = technical_score(indicator_row)
        news_opp = _float(row.get("opportunity_score"))
        news_risk = _float(row.get("risk_score"))
        combined_opp = round(max(news_opp + tech_score, 0), 4)
        combined_risk = round(news_risk + max(-tech_score, 0), 4)
        label = classify_opportunity(combined_opp, combined_risk)
        reasons = [
            f"news opportunity={round(news_opp, 2)} risk={round(news_risk, 2)}",
            f"articles={int(_float(row.get('article_count')))}",
        ] + tech_reasons
        key = f"{selected_run_time.isoformat()}|{window_hours}|{ticker}"

        signals.append(
            {
                "opportunity_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.opportunity:{key}")),
                "run_time": selected_run_time,
                "window_hours": window_hours,
                "ticker": ticker,
                "latest_date": indicator_row.get("date"),
                "news_opportunity_score": round(news_opp, 4),
                "news_risk_score": round(news_risk, 4),
                "technical_score": tech_score,
                "opportunity_score": combined_opp,
                "risk_score": combined_risk,
                "signal_label": label,
                "reasons": reasons[:8],
            }
        )

    return sorted(signals, key=lambda r: (-max(r["opportunity_score"], r["risk_score"]), r["ticker"]))


def upsert_opportunity_signals(engine, signals: list[dict]) -> None:
    if not signals:
        return
    sql = text(f"""
        INSERT INTO {OPPORTUNITY_TABLE} (
            opportunity_id, run_time, window_hours, ticker, latest_date,
            news_opportunity_score, news_risk_score, technical_score,
            opportunity_score, risk_score, signal_label, reasons, created_at
        )
        VALUES (
            :opportunity_id, :run_time, :window_hours, :ticker, :latest_date,
            :news_opportunity_score, :news_risk_score, :technical_score,
            :opportunity_score, :risk_score, :signal_label,
            CAST(:reasons AS text[]), now()
        )
        ON CONFLICT (opportunity_id)
        DO UPDATE SET
            latest_date = EXCLUDED.latest_date,
            news_opportunity_score = EXCLUDED.news_opportunity_score,
            news_risk_score = EXCLUDED.news_risk_score,
            technical_score = EXCLUDED.technical_score,
            opportunity_score = EXCLUDED.opportunity_score,
            risk_score = EXCLUDED.risk_score,
            signal_label = EXCLUDED.signal_label,
            reasons = EXCLUDED.reasons,
            created_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, signals)


def generate_opportunity_report(
    engine,
    *,
    window_hours: int,
    dry_run: bool = True,
    universe: str = "watchlist",
    include_meme: bool = False,
) -> list[dict]:
    run_time = datetime.now(timezone.utc)
    selected_universe = normalize_universe(universe)
    news_df = fetch_ticker_news_signals(engine, window_hours=window_hours, run_time=run_time)
    tickers = sorted({str(ticker).upper() for ticker in news_df.get("ticker", []) if str(ticker).strip()})
    indicators_df = fetch_latest_equity_indicators(engine, tickers=tickers) if tickers else pd.DataFrame()
    signals = build_opportunity_signals(
        news_df,
        indicators_df,
        window_hours=window_hours,
        run_time=run_time,
        universe=selected_universe,
        include_meme=include_meme,
    )
    if not dry_run:
        setup_opportunity_schema(engine)
        upsert_opportunity_signals(engine, signals)
    return signals
