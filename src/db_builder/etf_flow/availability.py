"""Point-in-time issuer availability helpers for ETF flow analytics."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig


def analysis_now() -> datetime:
    return datetime.now(timezone.utc)


def available_mask(rows: pd.DataFrame, analysis_timestamp: datetime) -> pd.Series:
    if rows.empty:
        return pd.Series(dtype=bool)
    published_source = rows["source_published_at"] if "source_published_at" in rows else pd.Series(pd.NaT, index=rows.index)
    ingested_source = rows["loaded_at"] if "loaded_at" in rows else pd.Series(pd.NaT, index=rows.index)
    published = pd.to_datetime(published_source, utc=True, errors="coerce")
    ingested = pd.to_datetime(ingested_source, utc=True, errors="coerce")
    available_at = published.fillna(ingested)
    return available_at.isna() | (available_at <= pd.Timestamp(analysis_timestamp))


def availability_status(coverage: float, *, stale: bool, config: ETFAnalyticsConfig) -> tuple[str, bool]:
    if stale:
        return "stale", True
    if coverage >= config.availability.complete_coverage_threshold:
        return "complete", False
    if coverage >= config.availability.high_coverage_threshold:
        return "provisional_high_coverage", True
    if coverage >= config.availability.minimum_usable_coverage:
        return "provisional_low_coverage", True
    return "unavailable", True


def issuer_availability_records(
    features: pd.DataFrame,
    *,
    analysis_timestamp: datetime,
    effective_date,
    config: ETFAnalyticsConfig,
) -> pd.DataFrame:
    if features.empty or "issuer" not in features:
        return pd.DataFrame()
    prior = features[pd.to_datetime(features["date"]).dt.date <= effective_date].copy()
    if prior.empty:
        return pd.DataFrame()
    latest = prior.sort_values(["issuer", "ticker", "date"]).groupby(["issuer", "ticker"], as_index=False).tail(1)
    available = available_mask(latest, analysis_timestamp)
    latest["availability_status"] = "REPORTED"
    latest.loc[~available, "availability_status"] = "NOT_YET_RELEASED"
    latest_dates = pd.to_datetime(latest["date"]).dt.date
    age_days = (pd.Timestamp(effective_date) - pd.to_datetime(latest_dates)).dt.days
    latest.loc[age_days > config.availability.stale_after_hours / 24.0, "availability_status"] = "STALE"
    rows = []
    for issuer, group in latest.groupby("issuer", dropna=False):
        reported = group[group["availability_status"].eq("REPORTED")]
        ingested_series = (
            pd.to_datetime(group["loaded_at"], utc=True, errors="coerce")
            if "loaded_at" in group
            else pd.Series(pd.NaT, index=group.index)
        )
        rows.append(
            {
                "analysis_timestamp": analysis_timestamp,
                "effective_date": effective_date,
                "issuer": issuer or "Unknown",
                "expected_release_at": None,
                "source_published_at": None,
                "ingested_at": ingested_series.max() if ingested_series.notna().any() else None,
                "availability_status": "REPORTED" if not reported.empty else str(group["availability_status"].iloc[0]),
                "eligible_aum": group["aum_lag1"].fillna(group["aum"]).sum(),
                "reported_aum": reported["aum_lag1"].fillna(reported["aum"]).sum() if not reported.empty else 0.0,
                "freshness_hours": float(age_days.min() * 24) if not age_days.empty else None,
                "data_quality_score": group["data_quality_score"].mean(),
            }
        )
    return pd.DataFrame(rows)
