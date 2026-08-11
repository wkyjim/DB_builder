"""Unified positioning and flow signal layer."""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
from sqlalchemy import text


BROAD_MARKET_SHORT_PRESSURE = {
    "SPY": "Broad Index",
    "QQQ": "Broad Index / Big Tech",
    "IWM": "Small Caps",
    "DIA": "Dow Industrials",
    "RSP": "Equal-Weight S&P 500",
}

SECTOR_SHORT_PRESSURE = {
    "XLK": "Technology",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "SMH": "Semiconductors",
    "SOXX": "Semiconductors",
    "CIBR": "Cybersecurity",
    "XAR": "Defense",
    "NLR": "Nuclear",
    "GRID": "Grid Infrastructure",
}

MAG7_SHORT_PRESSURE = {
    "AAPL": "Mag 7",
    "MSFT": "Mag 7",
    "NVDA": "Mag 7 / Semiconductors",
    "AMZN": "Mag 7 / Consumer Internet",
    "META": "Mag 7 / Communication Services",
    "GOOGL": "Mag 7 / Communication Services",
    "GOOG": "Mag 7 / Communication Services",
    "TSLA": "Mag 7 / High Beta",
}

HIGH_BETA_CHIP_SHORT_PRESSURE = {
    "MU": "High Beta Chips / Memory",
    "NVDA": "High Beta Chips / Mag 7",
    "SPCX": "High Beta / Space",
}

SHORT_PRESSURE_UNIVERSE = {
    **BROAD_MARKET_SHORT_PRESSURE,
    **SECTOR_SHORT_PRESSURE,
    **MAG7_SHORT_PRESSURE,
    **HIGH_BETA_CHIP_SHORT_PRESSURE,
}

BROAD_MARKET_ETF_FLOW_GROUPS = {
    "Broad Equity",
    "Total U.S. Equity",
    "Growth / Nasdaq",
    "Dow Industrials",
    "Equal-Weight Equity",
    "Small Caps",
    "Mid Caps",
    "Large / Mid Caps",
    "S&P 500 Growth",
    "S&P 500 Value",
    "Global Equity",
    "International Equity",
    "Developed Markets ex-US",
    "Emerging Markets",
    "Growth",
    "Value",
}

MACRO_ETF_FLOW_GROUPS = {
    "Bitcoin",
    "Gold",
    "Silver",
    "Core Bonds",
    "Treasury Bills",
    "U.S. Treasuries",
    "Municipal Bonds",
    "Mortgage Bonds",
    "High Yield Credit",
    "Investment Grade Credit",
    "Long Duration Treasury",
    "Intermediate Treasury",
    "Short Treasury",
}

ALWAYS_SHOW_ETF_FLOW_TICKERS = {"SMH", "SOXX"}


def setup_positioning_flow_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.positioning_flow_signals (
            signal_date date NOT NULL,
            asset_type text NOT NULL,
            asset_id text NOT NULL,
            signal_name text NOT NULL,
            signal_value numeric,
            z_score numeric,
            percentile numeric,
            interpretation text,
            source text NOT NULL,
            created_at timestamptz DEFAULT now(),
            PRIMARY KEY (signal_date, asset_id, signal_name, source)
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)


def _clean_numeric(value):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    if isinstance(value, Decimal):
        return numeric
    return value


def setup_flow_tables(engine) -> None:
    from db_builder.cot_positions import setup_cot_schema
    from db_builder.etf_flows import setup_etf_flow_schema
    from db_builder.finra_short_volume import setup_finra_short_volume_schema
    from db_builder.flow_sources import setup_flow_source_health_schema

    setup_flow_source_health_schema(engine)
    setup_cot_schema(engine)
    setup_finra_short_volume_schema(engine)
    setup_etf_flow_schema(engine)
    setup_positioning_flow_schema(engine)


def _cot_interpretation(row: dict) -> str:
    if row.get("crowded_long"):
        return "Crowded long positioning; unwind risk if price momentum weakens."
    if row.get("crowded_short"):
        return "Crowded short positioning; squeeze risk if price momentum improves."
    z = row.get("net_position_z_3y")
    if z is None:
        return "Positioning level available; z-score history still building."
    if float(z) > 1:
        return "Net long positioning is above normal."
    if float(z) < -1:
        return "Net short positioning is below normal."
    return "Positioning is near its rolling norm."


def build_cot_signals(cot_rows: list[dict]) -> list[dict]:
    signals = []
    for row in cot_rows:
        if not row.get("report_date") or not row.get("contract_code"):
            continue
        signals.append(
            {
                "signal_date": row["report_date"],
                "asset_type": row.get("asset_class") or "futures",
                "asset_id": row.get("asset_id") or row["contract_code"],
                "signal_name": "COT net position pct open interest",
                "signal_value": row.get("net_position_pct_oi"),
                "z_score": _clean_numeric(row.get("net_position_z_3y")),
                "percentile": _clean_numeric(row.get("percentile_3y")),
                "interpretation": _cot_interpretation(row),
                "source": "CFTC COT",
            }
        )
    return signals


def _finra_interpretation(row: dict) -> str:
    if row.get("short_covering_candidate"):
        return "Elevated short-sale volume with positive price action; possible short-covering candidate."
    if row.get("bearish_pressure_flag"):
        return "Elevated short-sale volume with negative price action; bearish pressure flag."
    if row.get("short_pressure_flag"):
        return "Elevated short-sale volume versus 60-day history; contested trading."
    return "Short-sale volume ratio available; no elevated z-score flag."


def short_pressure_group(ticker: str | None) -> str | None:
    if not ticker:
        return None
    return SHORT_PRESSURE_UNIVERSE.get(str(ticker).upper())


def short_pressure_implication(row: dict) -> str:
    ticker = str(row.get("asset_id") or row.get("ticker") or "").upper()
    group = row.get("asset_group") or short_pressure_group(ticker) or "Curated Market Exposure"
    z_score = _as_float(row.get("z_score") or row.get("short_volume_z_60d"), 0.0)
    ratio = _as_float(row.get("signal_value") or row.get("short_volume_ratio"), 0.0)
    price_move = _as_float(row.get("pct_chg"), None)
    if z_score >= 2 and price_move is not None and price_move < 0:
        return f"{group}: elevated short-sale pressure with negative price confirmation."
    if z_score >= 2 and price_move is not None and price_move > 0:
        return f"{group}: elevated short-sale pressure with positive price action; possible short-covering context."
    if z_score >= 2:
        return f"{group}: elevated short-sale pressure; watch for broader risk or squeeze confirmation."
    if ratio >= 0.5:
        return f"{group}: high short-sale volume ratio, but not statistically elevated versus its own history."
    return f"{group}: monitored exposure; no elevated short-pressure signal."


def _as_float(value, default=None):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(numeric) or math.isinf(numeric):
        return default
    return numeric


def short_pressure_priority(row: dict) -> float:
    ticker = str(row.get("asset_id") or row.get("ticker") or "").upper()
    z_score = max(_as_float(row.get("z_score") or row.get("short_volume_z_60d"), 0.0), 0.0)
    ratio = max(_as_float(row.get("signal_value") or row.get("short_volume_ratio"), 0.0), 0.0)
    price_move = abs(_as_float(row.get("pct_chg"), 0.0))
    universe_bonus = 10.0 if ticker in BROAD_MARKET_SHORT_PRESSURE else 0.0
    universe_bonus += 7.5 if ticker in SECTOR_SHORT_PRESSURE else 0.0
    universe_bonus += 8.0 if ticker in MAG7_SHORT_PRESSURE else 0.0
    universe_bonus += 8.5 if ticker in HIGH_BETA_CHIP_SHORT_PRESSURE else 0.0
    elevated_bonus = 15.0 if z_score >= 2.0 else 0.0
    return universe_bonus + elevated_bonus + (z_score * 4.0) + (ratio * 5.0) + min(price_move, 10.0)


def filter_meaningful_short_pressure_rows(rows: list[dict], *, limit: int = 15) -> list[dict]:
    curated = []
    for row in rows:
        ticker = str(row.get("asset_id") or row.get("ticker") or "").upper()
        group = short_pressure_group(ticker)
        if not group:
            continue
        enriched = dict(row)
        enriched["asset_id"] = ticker
        enriched["asset_group"] = group
        enriched["priority_score"] = round(short_pressure_priority(enriched), 4)
        enriched["market_implication"] = short_pressure_implication(enriched)
        curated.append(enriched)
    curated.sort(
        key=lambda row: (
            _as_float(row.get("priority_score"), 0.0),
            _as_float(row.get("z_score"), -999.0),
            _as_float(row.get("signal_value"), 0.0),
        ),
        reverse=True,
    )
    return curated[:limit]


def _flow_direction(value) -> str:
    numeric = _as_float(value, None)
    if numeric is None:
        return "n/a"
    if numeric > 0:
        return "inflow"
    if numeric < 0:
        return "outflow"
    return "flat"


def _etf_flow_bucket(category: str) -> str:
    if category in BROAD_MARKET_ETF_FLOW_GROUPS:
        return "Broad Market ETF Flows"
    if category in MACRO_ETF_FLOW_GROUPS:
        return "Fixed Income / Macro ETF Flows"
    return "Sector / Thematic ETF Flows"


def enrich_etf_flow_dashboard_rows(rows: list[dict], *, limit: int = 30) -> list[dict]:
    from db_builder.etf_flows import ETF_FLOW_UNIVERSE

    enriched_rows = []
    for row in rows:
        ticker = str(row.get("asset_id") or "").upper()
        category = ETF_FLOW_UNIVERSE.get(ticker, "ETF")
        one_day = _as_float(row.get("signal_value"), None)
        five_day = _as_float(row.get("z_score"), None)
        if five_day is None and (one_day is None or one_day == 0) and ticker not in ALWAYS_SHOW_ETF_FLOW_TICKERS:
            continue
        enriched = dict(row)
        enriched["asset_id"] = ticker
        enriched["asset_group"] = category
        enriched["display_name"] = f"{ticker} - {category}"
        enriched["flow_bucket"] = _etf_flow_bucket(category)
        flow_method = str(row.get("flow_method") or "")
        if ticker in ALWAYS_SHOW_ETF_FLOW_TICKERS and five_day is None and (one_day is None or one_day == 0):
            enriched["flow_comment"] = "Issuer-backed snapshot saved; flow history is still building."
        elif flow_method == "shares_delta_zero_no_creation_redemption":
            enriched["flow_comment"] = (
                "No 1D shares-outstanding change reported; "
                f"5D {_flow_direction(five_day)}."
            )
        else:
            enriched["flow_comment"] = f"1D {_flow_direction(one_day)}; 5D {_flow_direction(five_day)}."
        enriched_rows.append(enriched)
    enriched_rows.sort(
        key=lambda row: (
            0 if row.get("flow_bucket") == "Broad Market ETF Flows" else 1,
            0 if row.get("flow_bucket") == "Fixed Income / Macro ETF Flows" else 1,
            -abs(_as_float(row.get("z_score"), 0.0)),
            -abs(_as_float(row.get("signal_value"), 0.0)),
        ),
    )
    broad = [row for row in enriched_rows if row.get("flow_bucket") == "Broad Market ETF Flows"][:10]
    macro = [row for row in enriched_rows if row.get("flow_bucket") == "Fixed Income / Macro ETF Flows"][:10]
    sector_candidates = [row for row in enriched_rows if row.get("flow_bucket") == "Sector / Thematic ETF Flows"]
    sector = sector_candidates[:10]
    included_sector_tickers = {row.get("asset_id") for row in sector}
    for ticker in sorted(ALWAYS_SHOW_ETF_FLOW_TICKERS - included_sector_tickers):
        extra = next((row for row in sector_candidates if row.get("asset_id") == ticker), None)
        if extra:
            sector.append(extra)
    return (broad + macro + sector)[:limit]


def build_finra_signals(finra_rows: list[dict]) -> list[dict]:
    signals = []
    for row in finra_rows:
        if not row.get("trade_date") or not row.get("ticker"):
            continue
        signals.append(
            {
                "signal_date": row["trade_date"],
                "asset_type": "equity",
                "asset_id": row["ticker"],
                "signal_name": "FINRA short-sale volume ratio",
                "signal_value": _clean_numeric(row.get("short_volume_ratio")),
                "z_score": _clean_numeric(row.get("short_volume_z_60d")),
                "percentile": None,
                "interpretation": _finra_interpretation(row),
                "source": "FINRA short-sale volume",
            }
        )
    return signals


def upsert_positioning_flow_signals(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    setup_positioning_flow_schema(engine)
    sql = text(
        """
        INSERT INTO public.positioning_flow_signals (
            signal_date, asset_type, asset_id, signal_name, signal_value,
            z_score, percentile, interpretation, source, created_at
        )
        VALUES (
            :signal_date, :asset_type, :asset_id, :signal_name, :signal_value,
            :z_score, :percentile, :interpretation, :source, now()
        )
        ON CONFLICT (signal_date, asset_id, signal_name, source)
        DO UPDATE SET
            asset_type = EXCLUDED.asset_type,
            signal_value = EXCLUDED.signal_value,
            z_score = EXCLUDED.z_score,
            percentile = EXCLUDED.percentile,
            interpretation = EXCLUDED.interpretation,
            created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def fetch_latest_cot_rows(engine) -> list[dict]:
    sql = text(
        """
        WITH latest AS (
            SELECT *
            FROM public.cot_positions
            WHERE report_date = (SELECT MAX(report_date) FROM public.cot_positions)
              AND asset_id IS NOT NULL
              AND market_name NOT ILIKE '%DIVIDEND%'
              AND market_name NOT ILIKE '%XRATE%'
        ),
        ranked AS (
            SELECT
                latest.*,
                ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY open_interest DESC NULLS LAST) AS rn
            FROM latest
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """
    )
    try:
        return pd.read_sql(sql, engine).to_dict(orient="records")
    except Exception:
        return []


def fetch_latest_finra_rows(engine) -> list[dict]:
    sql = text(
        """
        SELECT *
        FROM public.finra_short_volume
        WHERE trade_date = (SELECT MAX(trade_date) FROM public.finra_short_volume)
        """
    )
    try:
        return pd.read_sql(sql, engine).to_dict(orient="records")
    except Exception:
        return []


def build_positioning_flow_signals_from_db(engine) -> list[dict]:
    from db_builder.etf_flows import build_etf_flow_signals, fetch_latest_etf_flow_rows

    return (
        build_cot_signals(fetch_latest_cot_rows(engine))
        + build_finra_signals(fetch_latest_finra_rows(engine))
        + build_etf_flow_signals(fetch_latest_etf_flow_rows(engine))
    )


def run_positioning_flow_signal_update(engine, *, dry_run: bool = False) -> dict:
    rows = build_positioning_flow_signals_from_db(engine)
    upserted = 0 if dry_run else upsert_positioning_flow_signals(engine, rows)
    return {"rows": len(rows), "upserted": upserted}


def fetch_positioning_flow_dashboard(engine, *, limit: int = 50) -> list[dict]:
    sql = text(
        """
        WITH cleaned AS (
            SELECT
                *,
                CASE WHEN z_score::text = 'NaN' THEN NULL ELSE z_score END AS valid_z_score
            FROM public.positioning_flow_signals
            WHERE signal_date >= (
                SELECT COALESCE(MAX(signal_date), CURRENT_DATE) - INTERVAL '14 days'
                FROM public.positioning_flow_signals
            )
              AND (
                  source <> 'FINRA short-sale volume'
                  OR asset_id = ANY(:short_pressure_tickers)
              )
              AND (
                  source NOT IN ('ETF flow proxy', 'ETF daily data')
                  OR signal_name IN ('ETF daily net flow proxy', 'ETF daily net fund flow')
              )
        )
        SELECT
            cleaned.*,
            p.pct_chg,
            p.close,
            p.volume
        FROM cleaned
        LEFT JOIN LATERAL (
            SELECT pct_chg, close, volume
            FROM public.us_equities
            WHERE ticker = cleaned.asset_id
            ORDER BY date DESC
            LIMIT 1
        ) p ON cleaned.source = 'FINRA short-sale volume'
        ORDER BY
            CASE source WHEN 'CFTC COT' THEN 0 WHEN 'ETF daily data' THEN 1 WHEN 'ETF flow proxy' THEN 1 WHEN 'FINRA short-sale volume' THEN 2 ELSE 3 END,
            COALESCE(valid_z_score, 0) DESC,
            ABS(COALESCE(signal_value, 0)) DESC,
            signal_date DESC
        LIMIT :limit
        """
    )
    try:
        rows = pd.read_sql(
            sql,
            engine,
            params={
                "limit": max(limit * 3, 100),
                "short_pressure_tickers": sorted(SHORT_PRESSURE_UNIVERSE),
            },
        ).to_dict(orient="records")
    except Exception:
        return []
    from db_builder.etf_flows import build_etf_flow_signals, fetch_latest_etf_flow_rows

    cftc = [row for row in rows if row.get("source") == "CFTC COT"][:12]
    etf_flow_candidates = build_etf_flow_signals(fetch_latest_etf_flow_rows(engine))
    etf_flows = enrich_etf_flow_dashboard_rows(etf_flow_candidates, limit=30)
    finra = filter_meaningful_short_pressure_rows(
        [row for row in rows if row.get("source") == "FINRA short-sale volume"],
        limit=max(limit - len(cftc) - len(etf_flows), 0),
    )
    other = [row for row in rows if row.get("source") not in {"CFTC COT", "ETF daily data", "ETF flow proxy", "FINRA short-sale volume"}]
    return (cftc + etf_flows + finra + other)[:limit]
