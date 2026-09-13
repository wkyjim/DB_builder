"""Latest-only short-positioning snapshot and Neon synchronization."""

from __future__ import annotations

import hashlib
import json
import math
import time
from numbers import Real
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_builder.equity_security_status import classify_security_type, create_equity_security_status_table
from db_builder.finra_short_analytics import setup_short_analytics_schema
from db_builder.short_regime import score_short_row


LATEST_TABLE = "public.us_equities_short_analytics_latest"
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "20260814_short_analytics_latest.sql"


def setup_short_snapshot_schema(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(MIGRATION.read_text(encoding="utf-8"))


def _security_type(ticker: str, name: object, existing: object) -> str:
    current = str(existing or "").strip().lower()
    if current and current not in {"common_or_fund", "unknown", "none", "nan"}:
        return current
    inferred = classify_security_type(ticker, None if pd.isna(name) else str(name))
    return "common_stock" if inferred == "common_or_fund" else inferred


def _safe_ratio(numerator: object, denominator: object) -> float | None:
    try:
        top, bottom = float(numerator), float(denominator)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(top) or not math.isfinite(bottom) or bottom == 0:
        return None
    return top / bottom


def _si_acceleration_state(row: pd.Series) -> str:
    slope = pd.to_numeric(row.get("si_slope_6m"), errors="coerce")
    acceleration = pd.to_numeric(row.get("si_acceleration"), errors="coerce")
    if pd.isna(slope) or pd.isna(acceleration):
        return "unavailable"
    if slope > 0 and acceleration > 0:
        return "accelerating_accumulation"
    if slope > 0 and acceleration < 0:
        return "decelerating_accumulation"
    if slope < 0 and acceleration < 0:
        return "accelerating_covering"
    if slope < 0 and acceleration > 0:
        return "covering"
    return "stable"


def _technical_states(row: pd.Series) -> tuple[str, str, str]:
    close = pd.to_numeric(row.get("latest_price"), errors="coerce")
    mas = [pd.to_numeric(row.get(f"ma{window}"), errors="coerce") for window in (20, 50, 100, 200)]
    valid = [value for value in mas if pd.notna(value)]
    if pd.notna(close) and len(valid) == 4 and close > mas[0] > mas[1] > mas[2] > mas[3]:
        trend = "bullish_alignment"
    elif pd.notna(close) and len(valid) == 4 and close < mas[0] < mas[1] < mas[2] < mas[3]:
        trend = "bearish_alignment"
    elif pd.notna(close) and valid and sum(close > value for value in valid) >= 3:
        trend = "partial_bullish"
    elif pd.notna(close) and valid and sum(close < value for value in valid) >= 3:
        trend = "partial_bearish"
    else:
        trend = "mixed"

    rsi = pd.to_numeric(row.get("rsi14"), errors="coerce")
    if pd.isna(rsi):
        rsi_state = "unavailable"
    elif rsi < 30:
        rsi_state = "oversold"
    elif rsi < 45:
        rsi_state = "weak"
    elif rsi < 55:
        rsi_state = "neutral"
    elif rsi < 70:
        rsi_state = "strengthening"
    else:
        rsi_state = "strong_extended"

    macd = pd.to_numeric(row.get("macd"), errors="coerce")
    signal = pd.to_numeric(row.get("macd_signal"), errors="coerce")
    histogram = pd.to_numeric(row.get("macd_histogram"), errors="coerce")
    if pd.isna(macd) or pd.isna(signal):
        macd_state = "unavailable"
    elif macd > signal and macd > 0:
        macd_state = "bullish_above_zero"
    elif macd > signal:
        macd_state = "bullish_below_zero"
    elif macd < signal and macd < 0:
        macd_state = "bearish_below_zero"
    else:
        macd_state = "bearish_above_zero"
    if pd.notna(histogram):
        macd_state += "_positive_histogram" if histogram > 0 else "_negative_histogram"
    return trend, rsi_state, macd_state


def _source_hash(row: pd.Series) -> str:
    excluded = {"source_hash", "snapshot_updated_at"}
    payload = {}
    for key, value in row.items():
        if key in excluded:
            continue
        if isinstance(value, Real) and not math.isfinite(float(value)):
            payload[key] = None
        elif value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            payload[key] = None
        elif hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    encoded = json.dumps(payload, sort_keys=True, default=str, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fetch_latest_snapshot_source(engine) -> pd.DataFrame:
    create_equity_security_status_table(engine)
    setup_short_analytics_schema(engine)
    sql = text(
        """
        WITH latest AS (
            SELECT DISTINCT ON (ticker) *
            FROM public.us_equities_short_analytics
            WHERE COALESCE(data_quality_status, 'valid') = 'valid'
            ORDER BY ticker, analytics_date DESC, calculated_at DESC
        ), latest_equity AS (
            SELECT DISTINCT ON (ticker) ticker, name, market
            FROM public.us_equities
            WHERE close IS NOT NULL AND close > 0
            ORDER BY ticker, date DESC
        ), latest_interval AS (
            SELECT DISTINCT ON (i.ticker) i.*
            FROM public.finra_short_interest_intervals i
            JOIN latest a ON a.ticker = i.ticker
            WHERE i.end_publication_date <= a.analytics_date
            ORDER BY i.ticker, i.end_publication_date DESC, i.end_settlement_date DESC
        )
        SELECT a.*,
               COALESCE(NULLIF(c.company_name, ''), NULLIF(s.name, ''), NULLIF(p.name, ''), raw_si.issue_name) AS name,
               CASE WHEN em.ticker IS NOT NULL THEN 'etf' ELSE s.security_type END AS existing_security_type,
               c.sector,
               c.industry,
               COALESCE(NULLIF(p.market, ''), raw_si.issuer_services_group_exchange_code) AS exchange,
               raw_si.previous_short_position_quantity AS previous_short_interest,
               raw_si.change_previous_number AS si_change_latest,
               raw_si.change_percent AS si_change_pct_latest,
               f.short_exempt_share_of_short,
               i.expected_si_direction,
               i.p_increase AS p_si_increase,
               i.p_flat AS p_si_flat,
               i.p_decrease AS p_si_decrease,
               i.flow_confirmation_signal,
               a.calculated_at AS source_calculated_at
        FROM latest a
        LEFT JOIN public.security_classification c ON c.ticker = a.ticker
        LEFT JOIN public.equity_security_status s ON s.ticker = a.ticker
        LEFT JOIN public.etf_master em ON em.ticker = a.ticker
        LEFT JOIN latest_equity p ON p.ticker = a.ticker
        LEFT JOIN public.finra_short_interest raw_si
          ON raw_si.ticker = a.ticker AND raw_si.settlement_date = a.si_settlement_date
        LEFT JOIN public.finra_short_volume_daily_features f
          ON f.ticker = a.ticker AND f.trade_date = a.analytics_date
        LEFT JOIN latest_interval i ON i.ticker = a.ticker
        """
    )
    return pd.read_sql(sql, engine)


def build_latest_snapshot(source: pd.DataFrame) -> pd.DataFrame:
    if source.empty:
        return source.copy()
    frame = source.copy()
    frame["security_type"] = frame.apply(
        lambda row: _security_type(row.get("ticker"), row.get("name"), row.get("existing_security_type")), axis=1
    )
    frame["latest_short_volume_date"] = frame["analytics_date"]
    frame["latest_si_settlement_date"] = frame["si_settlement_date"]
    frame["latest_si_publication_date"] = frame["si_publication_date"]
    frame["latest_price"] = frame["close"]
    frame["short_activity_persistence"] = frame.get("persistence_above_75p_20d")
    for window in (20, 50, 100, 200):
        frame[f"price_vs_ma{window}_pct"] = frame.apply(
            lambda row, window=window: (
                None
                if _safe_ratio(row.get("latest_price"), row.get(f"ma{window}")) is None
                else (_safe_ratio(row.get("latest_price"), row.get(f"ma{window}")) - 1) * 100
            ),
            axis=1,
        )
    states = frame.apply(_technical_states, axis=1, result_type="expand")
    states.columns = ["trend_structure", "rsi_state", "macd_state"]
    frame[states.columns] = states
    frame["si_acceleration_state"] = frame.apply(_si_acceleration_state, axis=1)
    frame["data_quality_score"] = frame.apply(
        lambda row: max(
            0,
            100
            - (25 if pd.isna(row.get("short_volume_ratio")) else 0)
            - (20 if pd.isna(row.get("latest_price")) else 0)
            - (20 if pd.isna(row.get("short_interest")) else 0)
            - (15 if pd.isna(row.get("rsi14")) else 0)
            - (20 if str(row.get("data_quality_status") or "valid") != "valid" else 0),
        ),
        axis=1,
    )
    for index, row in frame.iterrows():
        if pd.isna(row.get("funding_short_quality_score")):
            scoring_row = row.copy()
            scoring_row["rsi_14"] = row.get("rsi14")
            frame.at[index, "funding_short_quality_score"] = score_short_row(scoring_row)["funding_short_quality_score"]
    frame["source_hash"] = frame.apply(_source_hash, axis=1)
    return frame


def _target_columns(engine) -> list[str]:
    query = text(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'us_equities_short_analytics_latest'
        ORDER BY ordinal_position
        """
    )
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(query) if row[0] != "snapshot_updated_at"]


def upsert_latest_snapshot(engine, frame: pd.DataFrame, *, chunk_size: int = 200) -> int:
    if frame.empty:
        return 0
    setup_short_snapshot_schema(engine)
    columns = _target_columns(engine)
    prepared = frame.reindex(columns=columns).replace([np.inf, -np.inf], np.nan)
    prepared = prepared.astype(object).where(pd.notna(prepared), None)
    if "regime_reason_json" in prepared.columns:
        prepared["regime_reason_json"] = prepared["regime_reason_json"].map(
            lambda value: json.dumps(value, sort_keys=True, default=str, allow_nan=False)
            if isinstance(value, dict) else value
        )
    rows = prepared.to_dict(orient="records")
    query = text(
        f"""
        INSERT INTO {LATEST_TABLE} ({', '.join(columns)}, snapshot_updated_at)
        VALUES ({', '.join('CAST(:regime_reason_json AS jsonb)' if col == 'regime_reason_json' else ':' + col for col in columns)}, now())
        ON CONFLICT (ticker) DO UPDATE SET
          {', '.join(f'{col} = EXCLUDED.{col}' for col in columns if col != 'ticker')},
          snapshot_updated_at = now()
        """
    )
    with engine.begin() as connection:
        for start in range(0, len(rows), chunk_size):
            connection.execute(query, rows[start : start + chunk_size])
    return len(rows)


def bulk_upsert_latest_snapshot(engine, frame: pd.DataFrame, *, chunk_size: int = 200) -> int:
    """Use a temporary staging table for efficient remote snapshot merges."""
    if frame.empty:
        return 0
    setup_short_snapshot_schema(engine)
    columns = _target_columns(engine)
    prepared = frame.reindex(columns=columns).replace([np.inf, -np.inf], np.nan)
    prepared = prepared.astype(object).where(pd.notna(prepared), None)
    if "regime_reason_json" in prepared.columns:
        prepared["regime_reason_json"] = prepared["regime_reason_json"].map(
            lambda value: json.dumps(value, sort_keys=True, default=str, allow_nan=False)
            if isinstance(value, dict) else value
        )
    update_columns = [column for column in columns if column != "ticker"]
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TEMP TABLE temp_short_latest (LIKE {LATEST_TABLE} INCLUDING DEFAULTS) ON COMMIT DROP"))
        prepared.to_sql(
            "temp_short_latest", connection, if_exists="append", index=False,
            chunksize=chunk_size, method="multi",
        )
        connection.execute(
            text(
                f"""
                INSERT INTO {LATEST_TABLE} ({', '.join(columns)}, snapshot_updated_at)
                SELECT {', '.join(columns)}, now() FROM temp_short_latest
                ON CONFLICT (ticker) DO UPDATE SET
                  {', '.join(f'{column} = EXCLUDED.{column}' for column in update_columns)},
                  snapshot_updated_at = now()
                """
            )
        )
    return len(prepared)


def refresh_local_latest_snapshot(engine) -> dict:
    source = fetch_latest_snapshot_source(engine)
    snapshot = build_latest_snapshot(source)
    upserted = upsert_latest_snapshot(engine, snapshot)
    return {
        "rows": len(snapshot),
        "upserted": upserted,
        "latest_analytics_date": snapshot["analytics_date"].max() if not snapshot.empty else None,
        "latest_si_publication_date": snapshot["latest_si_publication_date"].dropna().max() if not snapshot.empty and snapshot["latest_si_publication_date"].notna().any() else None,
    }


def sync_latest_snapshot_to_neon(local_engine, neon_engine, *, retries: int = 3, chunk_size: int = 200) -> dict:
    setup_short_snapshot_schema(local_engine)
    setup_short_snapshot_schema(neon_engine)
    local = pd.read_sql(text(f"SELECT * FROM {LATEST_TABLE} ORDER BY ticker"), local_engine)
    remote_hashes = pd.read_sql(text(f"SELECT ticker, source_hash FROM {LATEST_TABLE}"), neon_engine)
    remote = dict(zip(remote_hashes.get("ticker", []), remote_hashes.get("source_hash", [])))
    changed = local[local.apply(lambda row: remote.get(row["ticker"]) != row["source_hash"], axis=1)].copy()
    inserted = sum(ticker not in remote for ticker in changed.get("ticker", []))
    updated = len(changed) - inserted
    if changed.empty:
        return {"local_rows": len(local), "uploaded": 0, "inserted": 0, "updated": 0, "failed": 0}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            print(f"Neon latest short snapshot: uploading changed_rows={len(changed):,} attempt={attempt}/{retries}")
            bulk_upsert_latest_snapshot(neon_engine, changed, chunk_size=chunk_size)
            sample = list(changed["ticker"].head(10))
            with neon_engine.connect() as connection:
                validated = connection.execute(
                    text(f"SELECT count(*) FROM {LATEST_TABLE} WHERE ticker = ANY(:tickers)"), {"tickers": sample}
                ).scalar_one()
            if validated != len(sample):
                raise RuntimeError("Neon sample validation did not return every uploaded ticker")
            return {
                "local_rows": len(local), "uploaded": len(changed), "inserted": inserted,
                "updated": updated, "failed": 0,
                "latest_analytics_date": local["analytics_date"].max(),
                "latest_si_publication_date": local["latest_si_publication_date"].dropna().max() if local["latest_si_publication_date"].notna().any() else None,
            }
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError("Latest short snapshot Neon sync failed") from last_error
