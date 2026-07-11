"""PostgreSQL repository for ETF flow analytics."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from db_builder.etf_flows import ETF_FLOW_UNIVERSE, ETF_ISSUER_REGISTRY


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "20260711_etf_flow_analytics.sql"


def setup_etf_flow_analytics_schema(engine) -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))


def _asset_class_for_segment(segment: str) -> str:
    if segment in {"Gold", "Silver"}:
        return "commodity"
    if segment == "Bitcoin":
        return "digital_asset"
    if any(token in segment for token in ["Treasury", "Bond", "Credit", "Yield", "Bills"]):
        return "fixed_income"
    return "equity"


def build_master_rows() -> list[dict[str, Any]]:
    rows = []
    for ticker, segment in ETF_FLOW_UNIVERSE.items():
        registry = ETF_ISSUER_REGISTRY.get(ticker, {})
        name = ticker
        rows.append(
            {
                "ticker": ticker,
                "fund_name": name,
                "issuer": registry.get("issuer"),
                "asset_class": _asset_class_for_segment(segment),
                "primary_segment": segment,
                "secondary_segment": None,
                "sector": segment if _asset_class_for_segment(segment) == "equity" else None,
                "theme": segment if ticker in {"SMH", "SOXX", "CIBR", "XAR", "NLR", "GRID", "IBIT"} else None,
                "style": segment if segment in {"Growth", "Value", "Quality Factor", "Dividend Growth"} else None,
                "region": "US" if segment not in {"Global Equity", "Developed Markets ex-US", "Emerging Markets", "International Equity"} else "Global",
                "duration_bucket": segment if "Treasury" in segment or "Duration" in segment else None,
                "credit_quality": segment if "Credit" in segment or "High Yield" in segment else None,
                "commodity_type": segment if segment in {"Gold", "Silver"} else None,
                "currency": "USD",
                "benchmark": None,
                "inception_date": None,
                "is_leveraged": False,
                "is_inverse": False,
                "is_active": True,
                "flow_eligible": True,
                "classification_version": "2026-07-11",
            }
        )
    return rows


def upsert_etf_master(engine, rows: list[dict[str, Any]] | None = None) -> int:
    setup_etf_flow_analytics_schema(engine)
    payload = rows or build_master_rows()
    if not payload:
        return 0
    sql = text(
        """
        INSERT INTO public.etf_master (
            ticker, fund_name, issuer, asset_class, primary_segment, secondary_segment,
            sector, theme, style, region, duration_bucket, credit_quality, commodity_type,
            currency, benchmark, inception_date, is_leveraged, is_inverse, is_active,
            flow_eligible, classification_version, updated_at
        )
        VALUES (
            :ticker, :fund_name, :issuer, :asset_class, :primary_segment, :secondary_segment,
            :sector, :theme, :style, :region, :duration_bucket, :credit_quality, :commodity_type,
            :currency, :benchmark, :inception_date, :is_leveraged, :is_inverse, :is_active,
            :flow_eligible, :classification_version, now()
        )
        ON CONFLICT (ticker)
        DO UPDATE SET
            issuer = EXCLUDED.issuer,
            asset_class = EXCLUDED.asset_class,
            primary_segment = EXCLUDED.primary_segment,
            secondary_segment = EXCLUDED.secondary_segment,
            sector = EXCLUDED.sector,
            theme = EXCLUDED.theme,
            style = EXCLUDED.style,
            region = EXCLUDED.region,
            duration_bucket = EXCLUDED.duration_bucket,
            credit_quality = EXCLUDED.credit_quality,
            commodity_type = EXCLUDED.commodity_type,
            is_active = EXCLUDED.is_active,
            flow_eligible = EXCLUDED.flow_eligible,
            classification_version = EXCLUDED.classification_version,
            updated_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def fetch_raw_etf_flow_source(engine, *, start_date=None, as_of_date=None) -> pd.DataFrame:
    setup_etf_flow_analytics_schema(engine)
    upsert_etf_master(engine)
    sql = """
        SELECT
            d.date,
            d.etf_ticker AS ticker,
            COALESCE(d.issuer, m.issuer) AS issuer,
            COALESCE(m.fund_name, d.etf_ticker) AS fund_name,
            m.asset_class,
            m.primary_segment,
            m.secondary_segment,
            m.sector,
            m.theme,
            m.style,
            m.region,
            m.duration_bucket,
            m.credit_quality,
            d.shares_outstanding,
            d.nav,
            COALESCE(d.market_price, d.nav) AS close,
            COALESCE(d.market_price, d.nav) AS adjusted_close,
            d.aum,
            NULL::numeric AS volume,
            COALESCE(m.currency, 'USD') AS currency,
            COALESCE(m.is_active, true) AS is_active,
            d.source,
            d.updated_at AS loaded_at
        FROM public.etf_daily_data d
        LEFT JOIN public.etf_master m ON m.ticker = d.etf_ticker
        WHERE (:start_date IS NULL OR d.date >= :start_date)
          AND (:as_of_date IS NULL OR d.date <= :as_of_date)
          AND COALESCE(m.flow_eligible, true) = true
          AND COALESCE(m.is_leveraged, false) = false
          AND COALESCE(m.is_inverse, false) = false
        ORDER BY d.etf_ticker, d.date
    """
    return pd.read_sql(text(sql), engine, params={"start_date": start_date, "as_of_date": as_of_date})


def upsert_raw_from_source(engine, raw: pd.DataFrame) -> int:
    if raw.empty:
        return 0
    rows = raw.where(pd.notna(raw), None).to_dict(orient="records")
    sql = text(
        """
        INSERT INTO public.etf_daily_raw (
            date, ticker, issuer, fund_name, asset_class, segment, sector, theme, style,
            region, duration_bucket, credit_quality, shares_outstanding, nav, close,
            adjusted_close, aum, volume, currency, is_active, source, loaded_at
        )
        VALUES (
            :date, :ticker, :issuer, :fund_name, :asset_class, :primary_segment, :sector, :theme, :style,
            :region, :duration_bucket, :credit_quality, :shares_outstanding, :nav, :close,
            :adjusted_close, :aum, :volume, :currency, :is_active, :source, now()
        )
        ON CONFLICT (date, ticker, source)
        DO UPDATE SET
            issuer = EXCLUDED.issuer,
            fund_name = EXCLUDED.fund_name,
            asset_class = EXCLUDED.asset_class,
            segment = EXCLUDED.segment,
            sector = EXCLUDED.sector,
            theme = EXCLUDED.theme,
            style = EXCLUDED.style,
            region = EXCLUDED.region,
            duration_bucket = EXCLUDED.duration_bucket,
            credit_quality = EXCLUDED.credit_quality,
            shares_outstanding = EXCLUDED.shares_outstanding,
            nav = EXCLUDED.nav,
            close = EXCLUDED.close,
            adjusted_close = EXCLUDED.adjusted_close,
            aum = EXCLUDED.aum,
            volume = EXCLUDED.volume,
            currency = EXCLUDED.currency,
            is_active = EXCLUDED.is_active,
            loaded_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def _records(df: pd.DataFrame, json_columns: set[str] | None = None) -> list[dict[str, Any]]:
    json_columns = json_columns or set()

    def clean(value):
        if value is None:
            return None
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if pd.isna(value):
            return None
        return value

    rows = []
    for record in df.where(pd.notna(df), None).to_dict(orient="records"):
        converted = {}
        for key, value in record.items():
            value = clean(value)
            if key in json_columns and value is not None:
                converted[key] = json.dumps(value, default=str, allow_nan=False)
            elif isinstance(value, list):
                converted[key] = value
            else:
                converted[key] = value
        rows.append(converted)
    return rows


def upsert_daily(engine, daily: pd.DataFrame) -> int:
    columns = [
        "date", "ticker", "issuer", "shares_outstanding", "shares_change", "nav", "nav_lag1",
        "close", "return_1d", "aum", "aum_lag1", "aum_avg_20d", "estimated_flow",
        "estimated_flow_lag_nav", "flow_pct_aum", "flow_pct_aum_lag", "flow_pct_aum_raw",
        "flow_pct_aum_winsorized", "volume", "data_quality_score", "flow_valid", "missing_data_flags",
    ]
    if daily.empty:
        return 0
    payload = _records(daily[[col for col in columns if col in daily.columns]])
    sql = text(
        """
        INSERT INTO public.etf_flow_daily (
            date, ticker, issuer, shares_outstanding, shares_change, nav, nav_lag1,
            close, return_1d, aum, aum_lag1, aum_avg_20d, estimated_flow,
            estimated_flow_lag_nav, flow_pct_aum, flow_pct_aum_lag, flow_pct_aum_raw,
            flow_pct_aum_winsorized, volume, data_quality_score, flow_valid, missing_data_flags,
            created_at
        )
        VALUES (
            :date, :ticker, :issuer, :shares_outstanding, :shares_change, :nav, :nav_lag1,
            :close, :return_1d, :aum, :aum_lag1, :aum_avg_20d, :estimated_flow,
            :estimated_flow_lag_nav, :flow_pct_aum, :flow_pct_aum_lag, :flow_pct_aum_raw,
            :flow_pct_aum_winsorized, :volume, :data_quality_score, :flow_valid, :missing_data_flags,
            now()
        )
        ON CONFLICT (date, ticker)
        DO UPDATE SET
            issuer = EXCLUDED.issuer,
            shares_outstanding = EXCLUDED.shares_outstanding,
            shares_change = EXCLUDED.shares_change,
            nav = EXCLUDED.nav,
            nav_lag1 = EXCLUDED.nav_lag1,
            close = EXCLUDED.close,
            return_1d = EXCLUDED.return_1d,
            aum = EXCLUDED.aum,
            aum_lag1 = EXCLUDED.aum_lag1,
            aum_avg_20d = EXCLUDED.aum_avg_20d,
            estimated_flow = EXCLUDED.estimated_flow,
            estimated_flow_lag_nav = EXCLUDED.estimated_flow_lag_nav,
            flow_pct_aum = EXCLUDED.flow_pct_aum,
            flow_pct_aum_lag = EXCLUDED.flow_pct_aum_lag,
            flow_pct_aum_raw = EXCLUDED.flow_pct_aum_raw,
            flow_pct_aum_winsorized = EXCLUDED.flow_pct_aum_winsorized,
            volume = EXCLUDED.volume,
            data_quality_score = EXCLUDED.data_quality_score,
            flow_valid = EXCLUDED.flow_valid,
            missing_data_flags = EXCLUDED.missing_data_flags,
            created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_features(engine, features: pd.DataFrame) -> int:
    columns = [
        "date", "ticker", "primary_segment", "issuer", "flow_1d", "flow_5d", "flow_20d", "flow_60d",
        "flow_5d_pct_aum", "flow_20d_pct_aum", "flow_60d_pct_aum", "flow_ema_5", "flow_ema_20",
        "flow_slope_5", "flow_slope_20", "flow_acceleration", "flow_zscore_20", "flow_zscore_60",
        "flow_percentile_252", "positive_flow_days_5d", "positive_flow_days_20d", "flow_persistence_20d",
        "return_1d", "return_5d", "return_20d", "price_trend_score", "price_flow_state", "data_quality_score",
    ]
    if features.empty:
        return 0
    payload = _records(features[[col for col in columns if col in features.columns]])
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(f"{col} = EXCLUDED.{col}" for col in columns if col not in {"date", "ticker"})
    sql = text(
        f"""
        INSERT INTO public.etf_flow_features ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (date, ticker)
        DO UPDATE SET {update_cols}, created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_segments(engine, segments: pd.DataFrame) -> int:
    columns = [
        "date", "segment_type", "segment", "flow_1d", "flow_5d", "flow_20d", "flow_60d",
        "flow_pct_aum_5d", "flow_pct_aum_20d", "flow_zscore_20d", "flow_momentum",
        "flow_breadth_20d", "issuer_consensus", "concentration_penalty", "score", "signal",
        "confidence", "top_contributors", "top_detractors", "eligible_fund_count", "issuer_count",
        "data_quality_score",
    ]
    if segments.empty:
        return 0
    payload = _records(segments[[col for col in columns if col in segments.columns]], {"top_contributors", "top_detractors"})
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(f"{col} = EXCLUDED.{col}" for col in columns if col not in {"date", "segment_type", "segment"})
    sql = text(
        f"""
        INSERT INTO public.etf_flow_segment_daily ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (date, segment_type, segment)
        DO UPDATE SET {update_cols}, created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_simple_table(engine, df: pd.DataFrame, table: str, key_columns: list[str], json_columns: set[str] | None = None) -> int:
    if df.empty:
        return 0
    columns = list(df.columns)
    payload = _records(df, json_columns)
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(f"{col} = EXCLUDED.{col}" for col in columns if col not in key_columns)
    sql = text(
        f"""
        INSERT INTO public.{table} ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT ({", ".join(key_columns)})
        DO UPDATE SET {update_cols}, created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_regime(engine, regime: dict[str, Any], as_of_date) -> int:
    if not as_of_date:
        return 0
    row = {
        "date": as_of_date,
        "existing_regime_score": regime.get("existing_regime_score"),
        "flow_regime_score": regime.get("score"),
        "combined_regime_score": regime.get("combined_regime_score"),
        "flow_regime_label": regime.get("label"),
        "flow_regime_confidence": regime.get("confidence"),
        "regime_conflict_flag": regime.get("conflict_flag"),
        "dominant_allocation_direction": regime.get("dominant_allocation_direction"),
        "largest_flow_contradiction": regime.get("largest_flow_contradiction"),
        "strongest_cross_issuer_theme": regime.get("strongest_cross_issuer_theme"),
        "largest_deterioration": regime.get("largest_deterioration"),
        "components": json.dumps(regime.get("components") or {}, default=str),
    }
    sql = text(
        """
        INSERT INTO public.etf_flow_regime_daily (
            date, existing_regime_score, flow_regime_score, combined_regime_score,
            flow_regime_label, flow_regime_confidence, regime_conflict_flag,
            dominant_allocation_direction, largest_flow_contradiction,
            strongest_cross_issuer_theme, largest_deterioration, components, created_at
        )
        VALUES (
            :date, :existing_regime_score, :flow_regime_score, :combined_regime_score,
            :flow_regime_label, :flow_regime_confidence, :regime_conflict_flag,
            :dominant_allocation_direction, :largest_flow_contradiction,
            :strongest_cross_issuer_theme, :largest_deterioration, :components, now()
        )
        ON CONFLICT (date)
        DO UPDATE SET
            existing_regime_score = EXCLUDED.existing_regime_score,
            flow_regime_score = EXCLUDED.flow_regime_score,
            combined_regime_score = EXCLUDED.combined_regime_score,
            flow_regime_label = EXCLUDED.flow_regime_label,
            flow_regime_confidence = EXCLUDED.flow_regime_confidence,
            regime_conflict_flag = EXCLUDED.regime_conflict_flag,
            dominant_allocation_direction = EXCLUDED.dominant_allocation_direction,
            largest_flow_contradiction = EXCLUDED.largest_flow_contradiction,
            strongest_cross_issuer_theme = EXCLUDED.strongest_cross_issuer_theme,
            largest_deterioration = EXCLUDED.largest_deterioration,
            components = EXCLUDED.components,
            created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, row)
    return 1


def fetch_latest_analytics_output(engine) -> dict[str, Any]:
    setup_etf_flow_analytics_schema(engine)
    regime = pd.read_sql(
        text("SELECT * FROM public.etf_flow_regime_daily ORDER BY date DESC LIMIT 1"),
        engine,
    ).to_dict(orient="records")
    segments = pd.read_sql(
        text(
            """
            SELECT *
            FROM public.etf_flow_segment_daily
            WHERE date = (SELECT MAX(date) FROM public.etf_flow_segment_daily)
            ORDER BY score DESC NULLS LAST
            LIMIT 15
            """
        ),
        engine,
    ).to_dict(orient="records")
    forward = pd.read_sql(
        text(
            """
            SELECT *
            FROM public.etf_flow_forward_signals
            WHERE date = (SELECT MAX(date) FROM public.etf_flow_forward_signals)
            ORDER BY outperformance_score DESC NULLS LAST
            LIMIT 12
            """
        ),
        engine,
    ).to_dict(orient="records")
    audits = pd.read_sql(
        text(
            """
            SELECT *
            FROM public.etf_flow_audit_flags
            WHERE date = (SELECT MAX(date) FROM public.etf_flow_audit_flags)
            ORDER BY severity DESC, flag_type
            LIMIT 12
            """
        ),
        engine,
    ).to_dict(orient="records")
    return {
        "flow_regime": regime[0] if regime else {},
        "market_segments": segments,
        "forward_signals": forward,
        "contradictions": audits,
    }
