"""Unified positioning and flow signal layer."""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
from sqlalchemy import text


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
    from db_builder.finra_short_volume import setup_finra_short_volume_schema
    from db_builder.flow_sources import setup_flow_source_health_schema

    setup_flow_source_health_schema(engine)
    setup_cot_schema(engine)
    setup_finra_short_volume_schema(engine)
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
    return build_cot_signals(fetch_latest_cot_rows(engine)) + build_finra_signals(fetch_latest_finra_rows(engine))


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
        )
        SELECT *
        FROM cleaned
        ORDER BY
            CASE source WHEN 'CFTC COT' THEN 0 WHEN 'FINRA short-sale volume' THEN 1 ELSE 2 END,
            ABS(COALESCE(valid_z_score, 0)) DESC,
            signal_date DESC
        LIMIT :limit
        """
    )
    try:
        return pd.read_sql(sql, engine, params={"limit": limit}).to_dict(orient="records")
    except Exception:
        return []
