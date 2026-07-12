"""PostgreSQL repository for ETF flow analytics."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from db_builder.etf_flows import ETF_FLOW_UNIVERSE, ETF_ISSUER_REGISTRY
from db_builder.etf_flow.exposure_mapping import attach_exposure_columns
from db_builder.etf_flow.representative import representative_rows


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
            attach_exposure_columns({
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
            })
        )
    return rows


def upsert_etf_master(engine, rows: list[dict[str, Any]] | None = None) -> int:
    setup_etf_flow_analytics_schema(engine)
    payload = rows or build_master_rows()
    payload = [row if row.get("exposure_id") else attach_exposure_columns(row) for row in payload]
    if not payload:
        return 0
    sql = text(
        """
        INSERT INTO public.etf_master (
            ticker, fund_name, issuer, asset_class, primary_segment, secondary_segment,
            sector, theme, style, region, duration_bucket, credit_quality, commodity_type,
            currency, benchmark, inception_date, is_leveraged, is_inverse, is_active,
            flow_eligible, classification_version, exposure_id, exposure_name, exposure_type,
            benchmark_family, is_primary_proxy, allocation_weight_cap, updated_at
        )
        VALUES (
            :ticker, :fund_name, :issuer, :asset_class, :primary_segment, :secondary_segment,
            :sector, :theme, :style, :region, :duration_bucket, :credit_quality, :commodity_type,
            :currency, :benchmark, :inception_date, :is_leveraged, :is_inverse, :is_active,
            :flow_eligible, :classification_version, :exposure_id, :exposure_name, :exposure_type,
            :benchmark_family, :is_primary_proxy, :allocation_weight_cap, now()
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
            exposure_id = EXCLUDED.exposure_id,
            exposure_name = EXCLUDED.exposure_name,
            exposure_type = EXCLUDED.exposure_type,
            benchmark_family = EXCLUDED.benchmark_family,
            is_primary_proxy = EXCLUDED.is_primary_proxy,
            allocation_weight_cap = EXCLUDED.allocation_weight_cap,
            updated_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_representative_map(engine, rows: list[dict[str, Any]] | None = None) -> int:
    setup_etf_flow_analytics_schema(engine)
    payload = rows or representative_rows()
    if not payload:
        return 0
    sql = text(
        """
        INSERT INTO public.etf_representative_map (
            exposure_id, exposure_name, exposure_type, primary_ticker, secondary_ticker,
            tertiary_ticker, primary_issuer, benchmark_family, selection_priority,
            aggregation_allowed, divergence_threshold_z, minimum_history_days,
            is_active, notes, updated_at
        )
        VALUES (
            :exposure_id, :exposure_name, :exposure_type, :primary_ticker, :secondary_ticker,
            :tertiary_ticker, :primary_issuer, :benchmark_family, :selection_priority,
            :aggregation_allowed, :divergence_threshold_z, :minimum_history_days,
            :is_active, :notes, now()
        )
        ON CONFLICT (exposure_id)
        DO UPDATE SET
            exposure_name = EXCLUDED.exposure_name,
            exposure_type = EXCLUDED.exposure_type,
            primary_ticker = EXCLUDED.primary_ticker,
            secondary_ticker = EXCLUDED.secondary_ticker,
            tertiary_ticker = EXCLUDED.tertiary_ticker,
            primary_issuer = EXCLUDED.primary_issuer,
            benchmark_family = EXCLUDED.benchmark_family,
            selection_priority = EXCLUDED.selection_priority,
            aggregation_allowed = EXCLUDED.aggregation_allowed,
            divergence_threshold_z = EXCLUDED.divergence_threshold_z,
            minimum_history_days = EXCLUDED.minimum_history_days,
            is_active = EXCLUDED.is_active,
            notes = EXCLUDED.notes,
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
        WITH ranked_source AS (
            SELECT
                d.*,
                ROW_NUMBER() OVER (
                    PARTITION BY d.date, d.etf_ticker
                    ORDER BY
                        CASE
                            WHEN d.source ILIKE '%navhist%' THEN 0
                            WHEN d.source ILIKE '%First Trust%' THEN 0
                            WHEN d.source ILIKE '%BlackRock historical%' THEN 0
                            WHEN d.source ILIKE '%BlackRock fund download%' THEN 1
                            WHEN d.source ILIKE '%VanEck%' THEN 1
                            ELSE 3
                        END,
                        CASE WHEN d.net_fund_flow_1d IS NULL THEN 1 ELSE 0 END,
                        ABS(COALESCE(d.net_fund_flow_1d, 0)) DESC,
                        d.updated_at DESC NULLS LAST
                ) AS source_rank
            FROM public.etf_daily_data d
            WHERE d.source NOT ILIKE '%yfinance%'
              AND d.source NOT ILIKE '%BlackRock fund download%'
        )
        SELECT
            d.date,
            d.etf_ticker AS ticker,
            COALESCE(d.issuer, m.issuer) AS issuer,
            COALESCE(m.fund_name, d.etf_ticker) AS fund_name,
            m.asset_class,
            m.primary_segment,
            m.exposure_id,
            m.exposure_name,
            m.exposure_type,
            m.benchmark_family,
            m.is_primary_proxy,
            m.allocation_weight_cap,
            m.secondary_segment,
            m.sector,
            m.theme,
            m.style,
            m.region,
            m.duration_bucket,
            m.credit_quality,
            d.shares_outstanding,
            d.nav,
            COALESCE(d.market_price, p.close, d.nav) AS close,
            COALESCE(d.market_price, p.close, d.nav) AS adjusted_close,
            d.aum,
            p.volume::numeric AS volume,
            COALESCE(m.currency, 'USD') AS currency,
            COALESCE(m.is_active, true) AS is_active,
            d.source,
            d.updated_at AS loaded_at
        FROM ranked_source d
        LEFT JOIN public.etf_master m ON m.ticker = d.etf_ticker
        LEFT JOIN public.us_equities p ON p.ticker = d.etf_ticker AND p.date = d.date
        WHERE (:start_date IS NULL OR d.date >= :start_date)
          AND (:as_of_date IS NULL OR d.date <= :as_of_date)
          AND d.source_rank = 1
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


def upsert_signal_daily(engine, signals: pd.DataFrame) -> int:
    columns = [
        "date", "ticker", "exposure_id", "exposure_type",
        "flow_1d", "flow_5d", "flow_20d", "flow_60d",
        "flow_pct_aum_1d", "flow_pct_aum_5d", "flow_pct_aum_20d", "flow_pct_aum_60d",
        "flow_zscore_1d", "flow_zscore_5d", "flow_zscore_20d", "flow_zscore_60d",
        "flow_percentile_20d", "flow_percentile_60d",
        "positive_flow_days_20d", "positive_flow_days_60d",
        "flow_persistence_20d", "flow_persistence_60d",
        "consecutive_inflow_days", "consecutive_outflow_days",
        "flow_momentum", "flow_acceleration", "flow_rotation_state",
        "volume_ratio_20d", "volume_ratio_60d", "volume_zscore_20d", "volume_zscore_60d",
        "dollar_volume", "dollar_volume_ratio_20d", "dollar_volume_zscore_60d",
        "price_state", "flow_state", "volume_state", "price_flow_volume_state",
        "state_strength", "state_confidence", "interpretation", "data_quality_score",
    ]
    if signals.empty:
        return 0
    payload = _records(signals[[col for col in columns if col in signals.columns]])
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(f"{col} = EXCLUDED.{col}" for col in columns if col not in {"date", "ticker"})
    sql = text(
        f"""
        INSERT INTO public.etf_flow_signal_daily ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (date, ticker)
        DO UPDATE SET {update_cols}, created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_market_flow(engine, market_flow: dict[str, Any]) -> int:
    if not market_flow:
        return 0
    columns = [
        "date", "equity_risk_flow_score", "credit_risk_flow_score",
        "sector_cyclicality_flow_score", "cash_preference_score",
        "duration_demand_score", "duration_liquidity_score", "gold_signal",
        "bitcoin_signal", "alternative_asset_score", "alternative_asset_interpretation",
        "market_flow_score", "market_flow_regime", "market_flow_reliability",
    ]
    row = {col: market_flow.get(col) for col in columns}
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(f"{col} = EXCLUDED.{col}" for col in columns if col != "date")
    sql = text(
        f"""
        INSERT INTO public.etf_market_flow_daily ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (date)
        DO UPDATE SET {update_cols}, created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, row)
    return 1


def upsert_divergence_flags(engine, flags: pd.DataFrame) -> int:
    columns = [
        "date", "flag_type", "severity", "exposure_id", "primary_ticker",
        "comparison_ticker", "description", "interpretation",
    ]
    if flags.empty:
        return 0
    payload = _records(flags[[col for col in columns if col in flags.columns]])
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(
        f"{col} = EXCLUDED.{col}"
        for col in columns
        if col not in {"date", "flag_type", "exposure_id", "primary_ticker", "comparison_ticker"}
    )
    sql = text(
        f"""
        INSERT INTO public.etf_flow_divergence_flags ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (date, flag_type, exposure_id, primary_ticker, comparison_ticker)
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


def upsert_exposures(engine, exposures: pd.DataFrame) -> int:
    columns = [
        "date", "analysis_timestamp", "exposure_id", "exposure_name", "exposure_type",
        "known_flow_1d", "known_flow_5d", "known_flow_20d",
        "normalized_flow_1d", "normalized_flow_5d", "normalized_flow_20d",
        "flow_momentum", "flow_acceleration", "flow_persistence",
        "reported_etf_count", "eligible_etf_count", "reported_issuer_count", "eligible_issuer_count",
        "issuer_aum_coverage", "data_availability_status", "issuer_agreement_score",
        "signal_reliability", "raw_flow_score", "adjusted_flow_score", "flow_signal", "is_provisional",
    ]
    if exposures.empty:
        return 0
    payload = _records(exposures[[col for col in columns if col in exposures.columns]])
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(
        f"{col} = EXCLUDED.{col}"
        for col in columns
        if col not in {"date", "analysis_timestamp", "exposure_id"}
    )
    sql = text(
        f"""
        INSERT INTO public.etf_flow_exposure_daily ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (date, analysis_timestamp, exposure_id)
        DO UPDATE SET {update_cols}, created_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_issuer_availability(engine, availability: pd.DataFrame) -> int:
    columns = [
        "analysis_timestamp", "effective_date", "issuer", "expected_release_at",
        "source_published_at", "ingested_at", "availability_status", "eligible_aum",
        "reported_aum", "freshness_hours", "data_quality_score",
    ]
    if availability.empty:
        return 0
    payload = _records(availability[[col for col in columns if col in availability.columns]])
    placeholders = ", ".join(f":{col}" for col in columns)
    update_cols = ", ".join(
        f"{col} = EXCLUDED.{col}"
        for col in columns
        if col not in {"analysis_timestamp", "effective_date", "issuer"}
    )
    sql = text(
        f"""
        INSERT INTO public.etf_issuer_data_availability ({", ".join(columns)}, created_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (analysis_timestamp, effective_date, issuer)
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
        text("SELECT * FROM public.etf_flow_regime_daily ORDER BY created_at DESC, date DESC LIMIT 1"),
        engine,
    ).to_dict(orient="records")
    segments = pd.read_sql(
        text(
            """
            SELECT *
            FROM public.etf_flow_exposure_daily
            WHERE (date, analysis_timestamp) = (
                SELECT date, analysis_timestamp
                FROM public.etf_flow_exposure_daily
                ORDER BY created_at DESC, analysis_timestamp DESC, date DESC
                LIMIT 1
            )
            ORDER BY adjusted_flow_score DESC NULLS LAST
            LIMIT 40
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
    market_flow = pd.read_sql(
        text("SELECT * FROM public.etf_market_flow_daily ORDER BY created_at DESC, date DESC LIMIT 1"),
        engine,
    ).to_dict(orient="records")
    representative = pd.read_sql(
        text(
            """
            WITH max_date AS (
                SELECT MAX(date) AS date FROM public.etf_flow_signal_daily
            ),
            ranked AS (
                SELECT
                    s.*,
                    ROW_NUMBER() OVER (PARTITION BY s.ticker ORDER BY s.date DESC) AS rn
                FROM public.etf_flow_signal_daily s
                WHERE s.date >= (SELECT date FROM max_date) - INTERVAL '5 days'
            )
            SELECT ranked.*, m.exposure_name
            FROM ranked
            LEFT JOIN public.etf_representative_map m
              ON m.exposure_id = ranked.exposure_id
             AND (
                 m.primary_ticker = ranked.ticker
                 OR m.secondary_ticker = ranked.ticker
                 OR m.tertiary_ticker = ranked.ticker
             )
            WHERE ranked.rn = 1
            ORDER BY ranked.state_strength DESC NULLS LAST, ranked.ticker
            LIMIT 80
            """
        ),
        engine,
    ).to_dict(orient="records")
    representative_divergences = pd.read_sql(
        text(
            """
            SELECT *
            FROM public.etf_flow_divergence_flags
            WHERE date = (SELECT MAX(date) FROM public.etf_flow_divergence_flags)
            ORDER BY severity DESC, exposure_id
            LIMIT 20
            """
        ),
        engine,
    ).to_dict(orient="records")
    return {
        "flow_regime": regime[0] if regime else {},
        "market_segments": segments,
        "exposures": segments,
        "forward_signals": forward,
        "contradictions": audits,
        "market_flow": market_flow[0] if market_flow else {},
        "representative_signals": representative,
        "representative_divergences": representative_divergences,
    }
