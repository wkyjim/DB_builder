"""Market Regime 2.0 driven by technical, momentum, breadth, risk, macro, and news inputs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text


MARKET_REGIME_V2_TABLE = "public.market_regime_v2"
KEY_ETFS = ["SPY", "QQQ", "IWM", "SMH"]
BREADTH_ETFS = [
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP",
    "XLU", "XLRE", "SMH", "SOXX", "CIBR", "XAR", "NLR", "GRID", "UTES",
]
MACRO_SYMBOLS = ["^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX", "^MOVE", "NQ=F", "ES=F", "RTY=F", "GC=F", "CL=F", "BZ=F", "BTC-USD", "ETH-USD", "^TNX", "^TYX", "DXY", "DX-Y.NYB", "HYG", "LQD", "JNK", "RSP", "IWF", "IWD", "TLT", "IEF", "SHY"]
RISK_ON_THEMES = {"AI", "Market Sentiment", "Mergers & Acquisitions", "Technology", "Semiconductors", "Financials", "Consumer Discretionary"}
RISK_OFF_THEMES = {"Geopolitics", "Oil", "Inflation", "Interest Rates", "Monetary Policy", "Regulation", "Credit Stress", "Trade Policy", "Tariffs"}


def setup_market_regime_v2_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {MARKET_REGIME_V2_TABLE} (
        signal_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        market_regime text,
        market_phase text,
        confidence numeric,
        macro_score numeric,
        technical_score numeric,
        momentum_score numeric,
        breadth_score numeric,
        risk_appetite_score numeric,
        news_score numeric,
        risk_on_score numeric,
        risk_off_score numeric,
        bullish_score numeric,
        bearish_score numeric,
        market_strength text,
        trend_state text,
        momentum_state text,
        volatility_state text,
        breadth_state text,
        risk_appetite_state text,
        drivers jsonb,
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_market_regime_v2_run_window
    ON {MARKET_REGIME_V2_TABLE} (run_time, window_hours);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def _float(value, default: float = 0.0) -> float:
    if value is None or pd.isna(value):
        return default
    return float(value)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def fetch_market_technical_inputs(engine, tickers: list[str] | None = None) -> pd.DataFrame:
    selected = tickers or sorted(set(KEY_ETFS + BREADTH_ETFS))
    sql = text("""
        WITH latest_raw AS (
            SELECT DISTINCT ON (ticker)
                ticker, date, close, pct_chg
            FROM public.us_equities
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
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
            r.ticker, r.date, r.close, r.pct_chg,
            i.indicator_date, i.ma_20, i.ma_50, i.ma_200,
            i.rsi_14, i.macd, i.macd_signal, i.macd_hist,
            i.return_5d, i.return_20d, i.return_60d
        FROM latest_raw r
        LEFT JOIN latest_indicators i ON i.ticker = r.ticker
    """)
    return pd.read_sql(sql, engine, params={"tickers": selected})


def fetch_macro_inputs(engine) -> pd.DataFrame:
    sql = text("""
        SELECT DISTINCT ON (symbol)
            symbol, name, asset_type, date, close, pct_chg
        FROM public.macro
        WHERE symbol = ANY(:symbols)
        ORDER BY symbol, date DESC
    """)
    return pd.read_sql(sql, engine, params={"symbols": MACRO_SYMBOLS})


def fetch_news_inputs(engine, *, window_hours: int, run_time: datetime | None = None) -> pd.DataFrame:
    selected_run_time = run_time or datetime.now(timezone.utc)
    sql = text("""
        SELECT dimension_type, dimension_value, article_count, weighted_sentiment_score, opportunity_score, risk_score
        FROM public.news_signals
        WHERE window_hours = :window_hours
          AND run_time >= (CAST(:run_time AS timestamptz) - (:window_hours * INTERVAL '1 hour'))
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "run_time": selected_run_time})


def score_row_trend(row: dict) -> float:
    close = _float(row.get("close"))
    if not close:
        return 50.0
    score = 50.0
    for column, weight in [("ma_20", 8), ("ma_50", 10), ("ma_200", 12)]:
        ma = _float(row.get(column))
        if ma:
            score += weight if close > ma else -weight
    rsi = _float(row.get("rsi_14"), 50.0)
    if 50 <= rsi <= 65:
        score += 8
    elif 65 < rsi <= 75:
        score += 5
    elif rsi > 75:
        score -= 6
    elif 35 <= rsi < 50:
        score -= 6
    elif rsi < 35:
        score -= 2
    macd_hist = _float(row.get("macd_hist"))
    if macd_hist > 0:
        score += 8
    elif macd_hist < 0:
        score -= 8
    return round(_clamp(score), 4)


def score_technical(technical_df: pd.DataFrame) -> tuple[float, str, list[str]]:
    rows = [row for row in technical_df.to_dict(orient="records") if row.get("ticker") in KEY_ETFS]
    if not rows:
        return 50.0, "neutral", ["technical data missing; neutral score"]
    scores = [score_row_trend(row) for row in rows]
    score = round(sum(scores) / len(scores), 4)
    state = "strong_uptrend" if score >= 75 else "uptrend" if score >= 60 else "neutral" if score >= 45 else "downtrend" if score >= 30 else "strong_downtrend"
    return score, state, [f"{row.get('ticker')} technical={score_row_trend(row)}" for row in rows[:4]]


def classify_momentum_state(rows: list[dict]) -> str:
    avg_5d = sum(_float(row.get("return_5d")) for row in rows) / max(len(rows), 1)
    avg_20d = sum(_float(row.get("return_20d")) for row in rows) / max(len(rows), 1)
    avg_60d = sum(_float(row.get("return_60d")) for row in rows) / max(len(rows), 1)
    if avg_5d > avg_20d > avg_60d and avg_60d > 0:
        return "accelerating"
    if avg_20d > 0 and avg_60d > 0:
        return "stable_positive"
    if avg_5d < 0 and avg_20d > 0 and avg_60d > 0:
        return "fading"
    if avg_5d > 0 and avg_20d < -8:
        return "oversold_rebound"
    if avg_5d < 0 or avg_20d < 0:
        return "negative"
    return "stable_positive"


def score_momentum(technical_df: pd.DataFrame) -> tuple[float, str, list[str]]:
    rows = [row for row in technical_df.to_dict(orient="records") if row.get("ticker") in KEY_ETFS]
    if not rows:
        return 50.0, "stable_positive", ["momentum data missing; neutral score"]
    avg = sum(_float(row.get("return_5d")) * 0.3 + _float(row.get("return_20d")) * 0.4 + _float(row.get("return_60d")) * 0.3 for row in rows) / len(rows)
    score = round(_clamp(50 + avg), 4)
    return score, classify_momentum_state(rows), [f"{row.get('ticker')} 5d={round(_float(row.get('return_5d')), 2)} 20d={round(_float(row.get('return_20d')), 2)} 60d={round(_float(row.get('return_60d')), 2)}" for row in rows[:4]]


def score_breadth(technical_df: pd.DataFrame) -> tuple[float, str, list[str]]:
    rows = technical_df.to_dict(orient="records")
    valid = [row for row in rows if _float(row.get("close")) and _float(row.get("ma_50")) and _float(row.get("ma_200"))]
    if not valid:
        return 50.0, "healthy", ["breadth data missing; ETF proxy neutral"]
    above_50 = sum(_float(row.get("close")) > _float(row.get("ma_50")) for row in valid) / len(valid)
    above_200 = sum(_float(row.get("close")) > _float(row.get("ma_200")) for row in valid) / len(valid)
    positive_20d = sum(_float(row.get("return_20d")) > 0 for row in valid) / len(valid)
    score = round((above_50 * 35) + (above_200 * 35) + (positive_20d * 30), 4)
    state = "broad_bull" if score >= 70 else "healthy" if score >= 55 else "weak" if score < 45 else "narrow"
    if score < 35:
        state = "very_weak"
    return score, state, [f"above_ma50={round(above_50 * 100, 1)}%", f"above_ma200={round(above_200 * 100, 1)}%", f"positive_20d={round(positive_20d * 100, 1)}%"]


def _macro_row(macro_df: pd.DataFrame, symbol: str) -> dict:
    matches = macro_df[macro_df["symbol"] == symbol]
    return matches.iloc[0].to_dict() if not matches.empty else {}


def score_risk_appetite(macro_df: pd.DataFrame, technical_df: pd.DataFrame) -> tuple[float, str, str, list[str]]:
    score = 50.0
    drivers = []
    vix = _macro_row(macro_df, "^VIX")
    vix_close = _float(vix.get("close"))
    vix_chg = _float(vix.get("pct_chg"))
    if vix_close >= 30 or vix_chg >= 10:
        score -= 25
        volatility = "stressed"
    elif vix_close >= 22 or vix_chg >= 4:
        score -= 12
        volatility = "elevated"
    elif vix_close <= 15 and vix_chg <= 0:
        score += 10
        volatility = "calm"
    else:
        volatility = "normal"
    if vix:
        drivers.append(f"VIX close={round(vix_close, 2)} pct_chg={round(vix_chg, 2)}")

    rows = {str(row.get("ticker")): row for row in technical_df.to_dict(orient="records")}
    spy = _float(rows.get("SPY", {}).get("return_20d"))
    for ticker, weight in [("IWM", 8), ("QQQ", 7), ("SMH", 10)]:
        rel = _float(rows.get(ticker, {}).get("return_20d")) - spy
        score += weight if rel > 0 else -weight if rel < -3 else 0
        drivers.append(f"{ticker}-SPY 20d relative={round(rel, 2)}")
    for symbol, weight in [("BTC-USD", 7), ("ETH-USD", 5)]:
        pct = _float(_macro_row(macro_df, symbol).get("pct_chg"))
        score += weight if pct > 0 else -weight if pct < -2 else 0
    gold = _float(_macro_row(macro_df, "GC=F").get("pct_chg"))
    spx = _float(_macro_row(macro_df, "^GSPC").get("pct_chg"))
    if gold > spx + 1:
        score -= 5
        drivers.append("gold outperforming equities")
    score = round(_clamp(score), 4)
    state = "risk_seeking" if score >= 65 else "neutral" if score >= 45 else "risk_reducing" if score >= 30 else "risk_aversion"
    return score, state, volatility, drivers[:8]


def score_macro(macro_df: pd.DataFrame) -> tuple[float, list[str]]:
    if macro_df.empty:
        return 50.0, ["macro data missing; neutral score"]
    score = 50.0
    drivers = []
    for symbol in ["^GSPC", "^IXIC", "NQ=F", "ES=F", "RTY=F"]:
        pct = _float(_macro_row(macro_df, symbol).get("pct_chg"))
        score += max(min(pct * 3, 8), -8)
        if pct:
            drivers.append(f"{symbol} pct_chg={round(pct, 2)}")
    for symbol in ["^TNX", "^TYX"]:
        pct = _float(_macro_row(macro_df, symbol).get("pct_chg"))
        if pct > 2:
            score -= 4
        elif pct < -2:
            score += 2
    return round(_clamp(score), 4), drivers[:8]


def score_news(news_df: pd.DataFrame) -> tuple[float, list[str]]:
    if news_df.empty:
        return 50.0, ["news signals missing; neutral score"]
    risk_on = 0.0
    risk_off = 0.0
    drivers = []
    for row in news_df.to_dict(orient="records"):
        if row.get("dimension_type") != "theme":
            continue
        theme = str(row.get("dimension_value"))
        if theme in RISK_ON_THEMES:
            risk_on += _float(row.get("opportunity_score"))
        if theme in RISK_OFF_THEMES:
            risk_off += _float(row.get("risk_score"))
        if theme in RISK_ON_THEMES | RISK_OFF_THEMES:
            drivers.append(f"{theme} opp={round(_float(row.get('opportunity_score')), 2)} risk={round(_float(row.get('risk_score')), 2)}")
    if risk_on + risk_off == 0:
        return 50.0, drivers[:8]
    score = 50 + ((risk_on - risk_off) / (risk_on + risk_off)) * 50
    return round(_clamp(score), 4), drivers[:8]


def classify_market_regime(score: float) -> str:
    if score >= 75:
        return "strong_risk_on"
    if score >= 60:
        return "risk_on"
    if score <= 25:
        return "strong_risk_off"
    if score <= 40:
        return "risk_off"
    return "neutral"


def classify_market_phase(trend_state: str, momentum_state: str, breadth_state: str, volatility_state: str) -> str:
    if volatility_state == "stressed" and momentum_state == "negative":
        return "capitulation"
    if trend_state in {"strong_downtrend", "downtrend"}:
        return "early_bear" if momentum_state == "negative" else "mid_bear"
    if trend_state in {"strong_uptrend", "uptrend"} and momentum_state == "fading":
        return "late_bull"
    if trend_state in {"strong_uptrend", "uptrend"} and breadth_state in {"broad_bull", "healthy"}:
        return "mid_bull"
    if trend_state in {"strong_uptrend", "uptrend"}:
        return "early_bull"
    if momentum_state == "negative":
        return "correction_in_bull"
    if trend_state == "neutral":
        return "range_bound"
    return "unclear"


def market_strength(score: float) -> str:
    distance = abs(score - 50)
    if distance >= 25:
        return "strong"
    if distance >= 10:
        return "moderate"
    return "weak"


def build_market_regime_v2(
    technical_df: pd.DataFrame,
    macro_df: pd.DataFrame,
    news_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> dict:
    selected_run_time = run_time or datetime.now(timezone.utc)
    technical_score, trend_state, technical_drivers = score_technical(technical_df)
    momentum_score, momentum_state, momentum_drivers = score_momentum(technical_df)
    breadth_score, breadth_state, breadth_drivers = score_breadth(technical_df)
    risk_appetite_score, risk_appetite_state, volatility_state, risk_drivers = score_risk_appetite(macro_df, technical_df)
    macro_score, macro_drivers = score_macro(macro_df)
    news_score, news_drivers = score_news(news_df)
    bullish_score = round(
        (technical_score * 0.25)
        + (momentum_score * 0.20)
        + (risk_appetite_score * 0.20)
        + (breadth_score * 0.15)
        + (macro_score * 0.10)
        + (news_score * 0.10),
        4,
    )
    bearish_score = round(100 - bullish_score, 4)
    regime = classify_market_regime(bullish_score)
    phase = classify_market_phase(trend_state, momentum_state, breadth_state, volatility_state)
    confidence = round(abs(bullish_score - 50) / 50, 4)
    key = f"{selected_run_time.isoformat()}|{window_hours}|{regime}|{phase}"
    drivers = {
        "technical": technical_drivers,
        "momentum": momentum_drivers,
        "breadth": breadth_drivers,
        "risk_appetite": risk_drivers,
        "macro": macro_drivers,
        "news": news_drivers,
    }
    return {
        "signal_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.market_regime_v2:{key}")),
        "run_time": selected_run_time,
        "window_hours": window_hours,
        "market_regime": regime,
        "market_phase": phase,
        "confidence": confidence,
        "macro_score": macro_score,
        "technical_score": technical_score,
        "momentum_score": momentum_score,
        "breadth_score": breadth_score,
        "risk_appetite_score": risk_appetite_score,
        "news_score": news_score,
        "risk_on_score": bullish_score,
        "risk_off_score": bearish_score,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "market_strength": market_strength(bullish_score),
        "trend_state": trend_state,
        "momentum_state": momentum_state,
        "volatility_state": volatility_state,
        "breadth_state": breadth_state,
        "risk_appetite_state": risk_appetite_state,
        "drivers": drivers,
    }


def upsert_market_regime_v2(engine, signal: dict) -> None:
    sql = text(f"""
        INSERT INTO {MARKET_REGIME_V2_TABLE} (
            signal_id, run_time, window_hours, market_regime, market_phase, confidence,
            macro_score, technical_score, momentum_score, breadth_score, risk_appetite_score,
            news_score, risk_on_score, risk_off_score, bullish_score, bearish_score,
            market_strength, trend_state, momentum_state, volatility_state, breadth_state,
            risk_appetite_state, drivers, created_at
        )
        VALUES (
            :signal_id, :run_time, :window_hours, :market_regime, :market_phase, :confidence,
            :macro_score, :technical_score, :momentum_score, :breadth_score, :risk_appetite_score,
            :news_score, :risk_on_score, :risk_off_score, :bullish_score, :bearish_score,
            :market_strength, :trend_state, :momentum_state, :volatility_state, :breadth_state,
            :risk_appetite_state, CAST(:drivers AS jsonb), now()
        )
        ON CONFLICT (signal_id)
        DO UPDATE SET
            market_regime = EXCLUDED.market_regime,
            market_phase = EXCLUDED.market_phase,
            confidence = EXCLUDED.confidence,
            macro_score = EXCLUDED.macro_score,
            technical_score = EXCLUDED.technical_score,
            momentum_score = EXCLUDED.momentum_score,
            breadth_score = EXCLUDED.breadth_score,
            risk_appetite_score = EXCLUDED.risk_appetite_score,
            news_score = EXCLUDED.news_score,
            risk_on_score = EXCLUDED.risk_on_score,
            risk_off_score = EXCLUDED.risk_off_score,
            bullish_score = EXCLUDED.bullish_score,
            bearish_score = EXCLUDED.bearish_score,
            market_strength = EXCLUDED.market_strength,
            trend_state = EXCLUDED.trend_state,
            momentum_state = EXCLUDED.momentum_state,
            volatility_state = EXCLUDED.volatility_state,
            breadth_state = EXCLUDED.breadth_state,
            risk_appetite_state = EXCLUDED.risk_appetite_state,
            drivers = EXCLUDED.drivers,
            created_at = now();
    """)
    row = signal.copy()
    row["drivers"] = json.dumps(row.get("drivers") or {}, default=str)
    with engine.begin() as conn:
        conn.execute(sql, row)


def generate_market_regime_v2(engine, *, window_hours: int, dry_run: bool = True) -> dict:
    run_time = datetime.now(timezone.utc)
    technical_df = fetch_market_technical_inputs(engine)
    macro_df = fetch_macro_inputs(engine)
    news_df = fetch_news_inputs(engine, window_hours=window_hours, run_time=run_time)
    signal = build_market_regime_v2(technical_df, macro_df, news_df, window_hours=window_hours, run_time=run_time)
    if not dry_run:
        setup_market_regime_v2_schema(engine)
        upsert_market_regime_v2(engine, signal)
    return signal
