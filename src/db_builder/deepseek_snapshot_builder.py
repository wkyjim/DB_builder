"""Build evidence-linked local market snapshots for the offline DeepSeek agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import text

from db_builder.news_importance import annotate_article_importance


TECH_TICKERS = ["SPY", "QQQ", "IWM", "SMH", "SOXX", "CIBR", "XLV", "XLF", "XLE", "XAR", "GRID", "NLR", "XLU", "XLY"]
APPROVED_TICKERS = set(TECH_TICKERS) | {"XLP", "XLRE", "XLI", "UTES", "TAN", "BTC-USD", "ETH-USD", "^VIX", "XLK"}
ETF_LABELS = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "IWM": "Russell 2000 ETF",
    "SMH": "Semiconductor ETF",
    "SOXX": "Semiconductor ETF",
    "CIBR": "Cybersecurity ETF",
    "XAR": "Defense ETF",
    "GRID": "Grid Infrastructure ETF",
    "NLR": "Nuclear ETF",
    "XLV": "Healthcare ETF",
    "XLF": "Financials ETF",
    "XLE": "Energy ETF",
    "XLU": "Utilities ETF",
    "XLY": "Consumer Discretionary ETF",
    "XLP": "Consumer Staples ETF",
    "XLRE": "Real Estate ETF",
    "XLI": "Industrials ETF",
    "UTES": "Utilities ETF",
    "TAN": "Solar ETF",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}


def _safe_read_sql(engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception:
        return pd.DataFrame()


def _records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    return [{key: _json_safe(value) for key, value in row.items()} for row in df.to_dict(orient="records")]


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        try:
            if pd.isna(value):
                return None
        except TypeError:
            pass
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _with_evidence(rows: list[dict], prefix: str) -> list[dict]:
    return [{**row, "evidence_id": f"{prefix}_{idx:03d}"} for idx, row in enumerate(rows, start=1)]


def _first(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


def _latest_run_query(table: str, window_hours: int, order_by: str, limit: int = 20) -> tuple[str, dict]:
    return (
        f"""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM {table}
            WHERE window_hours = :window_hours
        )
        SELECT s.*
        FROM {table} s
        JOIN latest_run r ON r.run_time = s.run_time
        WHERE s.window_hours = :window_hours
        ORDER BY {order_by}
        LIMIT :limit
        """,
        {"window_hours": window_hours, "limit": limit},
    )


def fetch_market_state(engine, *, window_hours: int) -> dict:
    df = _safe_read_sql(
        engine,
        """
        SELECT *
        FROM public.market_regime_v2
        WHERE window_hours = :window_hours
        ORDER BY run_time DESC
        LIMIT 1
        """,
        {"window_hours": window_hours},
    )
    row = _first(_records(df))
    if row:
        row["evidence_id"] = "REGIME_001"
    return row


def fetch_cross_asset(engine) -> list[dict]:
    df = _safe_read_sql(
        engine,
        """
        SELECT DISTINCT ON (symbol)
            symbol, name, asset_type, date, close, pct_chg
        FROM public.macro
        WHERE close IS NOT NULL
        ORDER BY symbol, date DESC
        """,
    )
    rows = _records(df)
    for row in rows:
        symbol = str(row.get("symbol") or "")
        asset_type = str(row.get("asset_type") or "")
        if symbol in {"^GSPC", "^IXIC", "^DJI", "^RUT", "NQ=F", "ES=F", "RTY=F"}:
            bucket = "equities"
        elif symbol in {"^TNX", "^TYX", "ZN=F", "ZB=F"} or "bond" in asset_type:
            bucket = "rates"
        elif symbol in {"GC=F", "CL=F", "BZ=F", "SI=F", "HG=F"}:
            bucket = "commodities"
        elif symbol in {"DXY", "DX-Y.NYB", "EURUSD", "USDJPY", "USDCNH"}:
            bucket = "fx"
        elif symbol in {"HYG", "LQD", "JNK"}:
            bucket = "credit"
        elif symbol in {"RSP", "IWF", "IWD"}:
            bucket = "equity_style"
        elif symbol in {"TLT", "IEF", "SHY", "^MOVE"}:
            bucket = "rates"
        elif symbol in {"BTC-USD", "ETH-USD"}:
            bucket = "crypto"
        elif symbol == "^VIX":
            bucket = "volatility"
        else:
            bucket = asset_type or "other"
        row["bucket"] = bucket
    return _with_evidence(rows, "MACRO")


def fetch_technicals(engine, *, tickers: list[str] | None = None) -> list[dict]:
    selected = tickers or TECH_TICKERS
    sql = """
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
                rsi_14,
                ma_20,
                ma_50,
                ma_200,
                return_5d,
                return_20d,
                return_60d,
                volatility_20d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers)
            ORDER BY ticker, date DESC
        )
        SELECT
            r.ticker,
            r.date,
            r.close,
            r.pct_chg,
            i.rsi_14,
            i.ma_20,
            i.ma_50,
            i.ma_200,
            i.return_5d,
            i.return_20d,
            i.return_60d,
            i.volatility_20d
        FROM latest_raw r
        LEFT JOIN latest_indicators i ON i.ticker = r.ticker
        ORDER BY r.ticker
    """
    rows = _records(_safe_read_sql(engine, sql, {"tickers": selected}))
    compact = []
    for row in rows:
        compact.append(
            {
                "ticker": row.get("ticker"),
                "label": ETF_LABELS.get(str(row.get("ticker")), str(row.get("ticker"))),
                "date": row.get("date"),
                "close": row.get("close"),
                "pct_chg": row.get("pct_chg"),
                "rsi": row.get("rsi_14"),
                "ma20": row.get("ma_20"),
                "ma50": row.get("ma_50"),
                "ma200": row.get("ma_200"),
                "return_5d": row.get("return_5d"),
                "return_20d": row.get("return_20d"),
                "return_60d": row.get("return_60d"),
                "volatility_20d": row.get("volatility_20d"),
            }
        )
    return _with_evidence(compact, "TECH")


def fetch_news(engine, *, window_hours: int, limit: int = 15) -> list[dict]:
    df = _safe_read_sql(
        engine,
        """
        SELECT
            a.article_id,
            a.title,
            a.summary,
            a.source_name,
            a.source_priority,
            a.source_category,
            a.published_at,
            a.fetched_at,
            c.sentiment_score,
            c.impact_score,
            c.confidence_score,
            c.themes,
            c.affected_tickers,
            c.reasoning
        FROM public.news_articles a
        LEFT JOIN public.news_classifications c ON c.article_id = a.article_id
        WHERE COALESCE(a.published_at, a.fetched_at, c.classified_at)
            >= (now() - (:window_hours * INTERVAL '1 hour'))
        ORDER BY COALESCE(a.source_priority, 0) DESC,
                 COALESCE(c.impact_score, 0) DESC,
                 COALESCE(a.published_at, a.fetched_at) DESC
        LIMIT :limit
        """,
        {"window_hours": window_hours, "limit": limit},
    )
    rows = []
    for row in _records(df):
        annotated = annotate_article_importance(row)
        rows.append(
            {
                "title": row.get("title"),
                "source": row.get("source_name"),
                "source_priority": row.get("source_priority"),
                "published_at": row.get("published_at"),
                "sentiment": row.get("sentiment_score"),
                "impact": row.get("impact_score"),
                "confidence": row.get("confidence_score"),
                "affected_tickers": row.get("affected_tickers") or [],
                "themes": row.get("themes") or [],
                "why_it_matters": row.get("reasoning") or "; ".join(annotated.get("importance_reasons", [])[:3]),
                "importance_score": annotated.get("importance_score"),
                "classification_priority": annotated.get("classification_priority"),
            }
        )
    return _with_evidence(rows, "NEWS")


def fetch_news_signals(engine, *, window_hours: int) -> list[dict]:
    sql, params = _latest_run_query(
        "public.news_signals",
        window_hours,
        "GREATEST(s.opportunity_score, s.risk_score) DESC, s.article_count DESC",
        limit=25,
    )
    return _with_evidence(_records(_safe_read_sql(engine, sql, params)), "RISK")


def fetch_sector_data(engine, *, window_hours: int) -> list[dict]:
    regimes_sql, params = _latest_run_query("public.sector_regimes", window_hours, "s.final_score DESC", limit=30)
    rotation_sql, _ = _latest_run_query("public.sector_rotation_signals", window_hours, "s.rotation_rank ASC", limit=30)
    sector_sql, _ = _latest_run_query("public.sector_signals", window_hours, "s.final_score DESC", limit=30)
    regimes = {row.get("sector_name"): row for row in _records(_safe_read_sql(engine, regimes_sql, params))}
    rotation = {row.get("sector_name"): row for row in _records(_safe_read_sql(engine, rotation_sql, params))}
    signals = {row.get("sector_name"): row for row in _records(_safe_read_sql(engine, sector_sql, params))}
    sectors = sorted(set(regimes) | set(rotation) | set(signals))
    rows = []
    for sector in sectors:
        regime = regimes.get(sector, {})
        rot = rotation.get(sector, {})
        sig = signals.get(sector, {})
        rows.append(
            {
                "sector": sector,
                "regime": regime.get("sector_regime"),
                "cycle_phase": regime.get("cycle_phase"),
                "rotation_rank": rot.get("rotation_rank"),
                "allocation_bias": rot.get("allocation_bias"),
                "recommended_action": rot.get("recommended_action"),
                "opportunity_score": sig.get("opportunity_score"),
                "risk_score": sig.get("risk_score") or regime.get("risk_score"),
                "final_score": regime.get("final_score") or sig.get("final_score"),
                "related_etfs": rot.get("related_etfs") or regime.get("related_etfs") or sig.get("related_etfs") or [],
            }
        )
    return _with_evidence(rows, "SECTOR")


def fetch_secular_themes(engine, *, window_hours: int) -> list[dict]:
    sql, params = _latest_run_query("public.secular_theme_signals", window_hours, "s.secular_score DESC", limit=20)
    rows = []
    for row in _records(_safe_read_sql(engine, sql, params)):
        rows.append(
            {
                "theme": row.get("theme_name"),
                "parent_theme": row.get("parent_theme"),
                "secular_score": row.get("secular_score"),
                "tactical_score": row.get("tactical_score"),
                "phase": row.get("theme_phase"),
                "confidence": row.get("confidence"),
                "related_etfs": row.get("related_etfs") or [],
                "top_subthemes": row.get("top_subthemes") or [],
            }
        )
    return _with_evidence(rows, "THEME")


def fetch_opportunities(engine, *, window_hours: int) -> list[dict]:
    sql, params = _latest_run_query("public.opportunity_signals", window_hours, "s.opportunity_score DESC", limit=15)
    return _with_evidence(_records(_safe_read_sql(engine, sql, params)), "OPPORTUNITY")


def confidence_inputs(snapshot: dict) -> list[dict]:
    market = snapshot.get("market_state") or {}
    signals = [
        {
            "input": "market_regime_v2_confidence",
            "value": market.get("confidence"),
            "interpretation": "Lower confidence reduces conviction and should soften directional language.",
        },
        {
            "input": "data_availability",
            "value": {
                "macro_items": len(snapshot.get("cross_asset", [])),
                "technical_items": len(snapshot.get("technicals", [])),
                "news_items": len(snapshot.get("news", [])),
                "sector_items": len(snapshot.get("sector_data", [])),
                "theme_items": len(snapshot.get("secular_themes", [])),
            },
            "interpretation": "Broader populated coverage improves report confidence.",
        },
        {
            "input": "opportunity_scanner",
            "value": "no high-conviction opportunities" if not snapshot.get("opportunity_risk", {}).get("top_opportunities") else "opportunities present",
            "interpretation": "No passing opportunities is a caution signal, not proof that no themes exist.",
        },
    ]
    return _with_evidence(signals, "CONFIDENCE")


def evidence_appendix(snapshot: dict) -> list[dict]:
    entries = []

    def add_rows(section: str, rows):
        if isinstance(rows, dict):
            rows = [rows] if rows.get("evidence_id") else []
        for row in rows or []:
            evidence_id = row.get("evidence_id")
            if not evidence_id:
                continue
            summary = "; ".join(
                f"{key}={value}" for key, value in row.items()
                if key != "evidence_id" and value not in (None, "", [])
            )
            entries.append({"evidence_id": evidence_id, "section": section, "summary": summary[:1200]})

    add_rows("market_state", snapshot.get("market_state"))
    add_rows("cross_asset", snapshot.get("cross_asset"))
    add_rows("technicals", snapshot.get("technicals"))
    add_rows("news", snapshot.get("news"))
    add_rows("sector_data", snapshot.get("sector_data"))
    add_rows("secular_themes", snapshot.get("secular_themes"))
    add_rows("top_opportunities", snapshot.get("opportunity_risk", {}).get("top_opportunities"))
    add_rows("top_risks", snapshot.get("opportunity_risk", {}).get("top_risks"))
    add_rows("confidence_inputs", snapshot.get("confidence_inputs"))
    return entries


def build_offline_snapshot(engine, *, window_hours: int) -> dict:
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": window_hours,
        "market_state": fetch_market_state(engine, window_hours=window_hours),
        "cross_asset": fetch_cross_asset(engine),
        "technicals": fetch_technicals(engine),
        "news": fetch_news(engine, window_hours=window_hours),
        "sector_data": fetch_sector_data(engine, window_hours=window_hours),
        "secular_themes": fetch_secular_themes(engine, window_hours=window_hours),
        "opportunity_risk": {
            "top_opportunities": fetch_opportunities(engine, window_hours=window_hours),
            "top_risks": fetch_news_signals(engine, window_hours=window_hours),
            "no_opportunity_warning": None,
        },
        "approved_ticker_universe": sorted(APPROVED_TICKERS),
        "approved_etf_label_map": ETF_LABELS,
        "unavailable_fields": {
            "investor_surveys": None,
            "fund_flows": None,
            "options_positioning": None,
        },
    }
    if not snapshot["opportunity_risk"]["top_opportunities"]:
        snapshot["opportunity_risk"]["no_opportunity_warning"] = "No high-conviction single-name opportunities passed strict filters."
    snapshot["confidence_inputs"] = confidence_inputs(snapshot)
    snapshot["evidence_appendix"] = evidence_appendix(snapshot)
    return snapshot
