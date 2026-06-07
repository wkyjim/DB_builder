"""Market regime scoring from local news signals and macro data."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text


REGIME_SIGNAL_TABLE = "public.market_regime_signals"


def setup_market_regime_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {REGIME_SIGNAL_TABLE} (
        regime_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        regime_label text,
        risk_on_score numeric,
        risk_off_score numeric,
        confidence_score numeric,
        news_signal_count integer,
        macro_signal_count integer,
        summary text,
        drivers text[],
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_market_regime_run_window
    ON {REGIME_SIGNAL_TABLE} (run_time, window_hours);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def fetch_news_regime_inputs(engine, *, window_hours: int, run_time: datetime | None = None) -> pd.DataFrame:
    selected_run_time = run_time or datetime.now(timezone.utc)
    sql = text("""
        SELECT
            dimension_type,
            dimension_value,
            article_count,
            high_impact_count,
            weighted_sentiment_score,
            opportunity_score,
            risk_score
        FROM public.news_signals
        WHERE window_hours = :window_hours
          AND run_time >= (CAST(:run_time AS timestamptz) - (:window_hours * INTERVAL '1 hour'))
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "run_time": selected_run_time})


def fetch_macro_regime_inputs(engine, *, window_hours: int, run_time: datetime | None = None) -> pd.DataFrame:
    selected_run_time = run_time or datetime.now(timezone.utc)
    lookback_days = max(2, int(window_hours / 24) + 2)
    sql = text("""
        SELECT DISTINCT ON (symbol)
            symbol,
            name,
            asset_type,
            date,
            pct_chg,
            close
        FROM public.macro
        WHERE date >= (CAST(:run_time AS date) - (:lookback_days * INTERVAL '1 day'))
          AND pct_chg IS NOT NULL
        ORDER BY symbol, date DESC
    """)
    return pd.read_sql(
        sql,
        engine,
        params={"run_time": selected_run_time, "lookback_days": lookback_days},
    )


def _float(value, default: float = 0.0) -> float:
    if pd.isna(value):
        return default
    return float(value)


def score_news_regime(news_df: pd.DataFrame) -> tuple[float, float, list[str]]:
    if news_df.empty:
        return 0.0, 0.0, []

    risk_on = 0.0
    risk_off = 0.0
    drivers = []
    risk_keywords = {
        "market sentiment",
        "ai",
        "fed",
        "rates",
        "oil",
        "defense",
        "nuclear",
        "monetary policy",
        "central banks",
        "interest rates",
        "inflation",
        "financial stability",
        "bank regulation",
        "capital markets",
        "etf regulation",
        "crypto regulation",
        "ai regulation",
        "trade policy",
        "fiscal policy",
        "treasury market",
        "labor market",
        "housing",
        "energy security",
        "national security",
        "defense spending",
        "geopolitics",
    }

    for row in news_df.to_dict(orient="records"):
        value = str(row.get("dimension_value", "")).lower()
        dimension_type = str(row.get("dimension_type", ""))
        if dimension_type == "theme" and value not in risk_keywords:
            continue

        opportunity = _float(row.get("opportunity_score"))
        risk = _float(row.get("risk_score"))
        article_count = int(_float(row.get("article_count")))
        weight = 1.0 + min(article_count, 5) * 0.1
        risk_on += opportunity * weight
        risk_off += risk * weight

        if max(opportunity, risk) >= 25:
            drivers.append(f"news:{row.get('dimension_value')} opp={round(opportunity, 2)} risk={round(risk, 2)}")

    return round(risk_on, 4), round(risk_off, 4), drivers[:8]


def score_macro_regime(macro_df: pd.DataFrame) -> tuple[float, float, list[str]]:
    if macro_df.empty:
        return 0.0, 0.0, []

    risk_on = 0.0
    risk_off = 0.0
    drivers = []

    for row in macro_df.to_dict(orient="records"):
        symbol = str(row.get("symbol", ""))
        asset_type = str(row.get("asset_type", ""))
        pct_chg = _float(row.get("pct_chg"))

        if symbol == "^VIX":
            if pct_chg > 0:
                risk_off += min(abs(pct_chg) * 5, 30)
            elif pct_chg < 0:
                risk_on += min(abs(pct_chg) * 3, 20)
        elif asset_type in {"stock_index", "crypto"} or symbol in {"ES=F", "NQ=F", "RTY=F"}:
            if pct_chg > 0:
                risk_on += min(abs(pct_chg) * 8, 25)
            elif pct_chg < 0:
                risk_off += min(abs(pct_chg) * 8, 25)
        elif asset_type == "ust_yield":
            if pct_chg > 0:
                risk_off += min(abs(pct_chg) * 3, 18)
            elif pct_chg < 0:
                risk_on += min(abs(pct_chg) * 2, 12)

        if abs(pct_chg) >= 0.5:
            drivers.append(f"macro:{symbol} pct_chg={round(pct_chg, 2)}")

    return round(risk_on, 4), round(risk_off, 4), drivers[:8]


def classify_regime(risk_on_score: float, risk_off_score: float) -> tuple[str, float]:
    total = risk_on_score + risk_off_score
    if total <= 0:
        return "neutral", 0.0

    spread = risk_on_score - risk_off_score
    confidence = round(min(abs(spread) / total, 1.0), 4)
    if spread >= 20:
        return "risk_on", confidence
    if spread <= -20:
        return "risk_off", confidence
    return "mixed", confidence


def build_market_regime_signal(
    news_df: pd.DataFrame,
    macro_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> dict:
    selected_run_time = run_time or datetime.now(timezone.utc)
    news_on, news_off, news_drivers = score_news_regime(news_df)
    macro_on, macro_off, macro_drivers = score_macro_regime(macro_df)
    risk_on_score = round(news_on + macro_on, 4)
    risk_off_score = round(news_off + macro_off, 4)
    label, confidence = classify_regime(risk_on_score, risk_off_score)
    drivers = (news_drivers + macro_drivers)[:12]
    summary = (
        f"{label} regime from risk_on={risk_on_score}, "
        f"risk_off={risk_off_score}, confidence={confidence}"
    )
    regime_key = f"{selected_run_time.isoformat()}|{window_hours}|{label}"

    return {
        "regime_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.market_regime:{regime_key}")),
        "run_time": selected_run_time,
        "window_hours": window_hours,
        "regime_label": label,
        "risk_on_score": risk_on_score,
        "risk_off_score": risk_off_score,
        "confidence_score": confidence,
        "news_signal_count": len(news_df),
        "macro_signal_count": len(macro_df),
        "summary": summary,
        "drivers": drivers,
    }


def upsert_market_regime_signal(engine, signal: dict) -> None:
    sql = text(f"""
        INSERT INTO {REGIME_SIGNAL_TABLE} (
            regime_id, run_time, window_hours, regime_label,
            risk_on_score, risk_off_score, confidence_score,
            news_signal_count, macro_signal_count, summary, drivers, created_at
        )
        VALUES (
            :regime_id, :run_time, :window_hours, :regime_label,
            :risk_on_score, :risk_off_score, :confidence_score,
            :news_signal_count, :macro_signal_count, :summary,
            CAST(:drivers AS text[]), now()
        )
        ON CONFLICT (regime_id)
        DO UPDATE SET
            regime_label = EXCLUDED.regime_label,
            risk_on_score = EXCLUDED.risk_on_score,
            risk_off_score = EXCLUDED.risk_off_score,
            confidence_score = EXCLUDED.confidence_score,
            news_signal_count = EXCLUDED.news_signal_count,
            macro_signal_count = EXCLUDED.macro_signal_count,
            summary = EXCLUDED.summary,
            drivers = EXCLUDED.drivers,
            created_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, signal)


def generate_market_regime(engine, *, window_hours: int, dry_run: bool = True) -> dict:
    run_time = datetime.now(timezone.utc)
    news_df = fetch_news_regime_inputs(engine, window_hours=window_hours, run_time=run_time)
    macro_df = fetch_macro_regime_inputs(engine, window_hours=window_hours, run_time=run_time)
    signal = build_market_regime_signal(news_df, macro_df, window_hours=window_hours, run_time=run_time)
    if not dry_run:
        setup_market_regime_schema(engine)
        upsert_market_regime_signal(engine, signal)
    return signal
