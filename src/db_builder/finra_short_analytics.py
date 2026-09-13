"""Vectorized FINRA short-volume, positioning, and regime analytics."""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right, insort
from collections import deque
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text

from db_builder.short_analytics_config import load_short_analytics_config


DAILY_FEATURE_COLUMNS = [
    "trade_date",
    "ticker",
    "short_volume_ratio",
    "short_exempt_ratio",
    "short_exempt_share_of_short",
    "svr_5d",
    "svr_20d",
    "svr_60d",
    "svr_z20",
    "svr_z60",
    "svr_pctile_5d_1y",
    "svr_pctile_20d_1y",
    "svr_pctile_60d_1y",
    "abnormal_svr_60d",
    "abnormal_svr_252d",
    "casv_5d",
    "casv_10d",
    "casv_20d",
    "svr_acceleration_5_20",
    "svr_acceleration_20_60",
    "persistence_above_median_20d",
    "persistence_above_median_60d",
    "persistence_above_75p_20d",
    "persistence_above_75p_60d",
    "persistence_above_90p_20d",
    "persistence_above_90p_60d",
    "short_activity_score",
    "data_quality_status",
]


def setup_short_analytics_schema(engine) -> None:
    migration = Path(__file__).resolve().parents[2] / "migrations" / "20260812_finra_short_analytics.sql"
    sql = migration.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = pd.to_numeric(denominator, errors="coerce").replace(0, np.nan)
    return pd.to_numeric(numerator, errors="coerce") / denominator


def _rolling_percentile(values: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Rank each observation against the strictly prior rolling window."""
    numeric = pd.to_numeric(values, errors="coerce")
    result = pd.Series(np.nan, index=values.index, dtype=float)
    history: deque[float] = deque()
    ordered: list[float] = []
    for index, value in numeric.items():
        if pd.notna(value) and len(ordered) >= min_periods:
            result.at[index] = bisect_right(ordered, float(value)) / len(ordered)
        if pd.notna(value):
            number = float(value)
            history.append(number)
            insort(ordered, number)
        if len(history) > window:
            expired = history.popleft()
            ordered.pop(bisect_left(ordered, expired))
    return result


def _lagged_zscore(values: pd.Series, window: int, min_periods: int) -> pd.Series:
    prior = values.shift(1)
    mean = prior.rolling(window, min_periods=min_periods).mean()
    std = prior.rolling(window, min_periods=min_periods).std().replace(0, np.nan)
    return (values - mean) / std


def _ticker_daily_features(group: pd.DataFrame, config: dict) -> pd.DataFrame:
    ticker = group.name
    group = group.sort_values("trade_date").copy()
    group["ticker"] = ticker
    ratio = pd.to_numeric(group["short_volume_ratio"], errors="coerce")
    minimum = int(config["history"]["minimum_daily_history"])
    history = int(config["history"]["daily_normalization_observations"])
    baseline = int(config["history"]["daily_baseline_observations"])

    group["short_exempt_ratio"] = _safe_divide(group["short_exempt_volume"], group["total_volume"])
    group["short_exempt_share_of_short"] = _safe_divide(group["short_exempt_volume"], group["short_volume"])
    for window in (5, 20, 60):
        group[f"svr_{window}d"] = ratio.rolling(window, min_periods=max(2, min(window, window // 2))).mean()

    group["svr_z20"] = _lagged_zscore(group["svr_20d"], history, minimum)
    group["svr_z60"] = _lagged_zscore(group["svr_60d"], history, minimum)
    group["svr_pctile_5d_1y"] = _rolling_percentile(group["svr_5d"], history, minimum)
    group["svr_pctile_20d_1y"] = _rolling_percentile(group["svr_20d"], history, minimum)
    group["svr_pctile_60d_1y"] = _rolling_percentile(group["svr_60d"], history, minimum)

    prior_ratio = ratio.shift(1)
    prior_median_60 = prior_ratio.rolling(baseline, min_periods=min(30, minimum)).median()
    prior_median_252 = prior_ratio.rolling(history, min_periods=minimum).median()
    prior_75 = prior_ratio.rolling(history, min_periods=minimum).quantile(0.75)
    prior_90 = prior_ratio.rolling(history, min_periods=minimum).quantile(0.90)
    group["abnormal_svr_60d"] = ratio - prior_median_60
    group["abnormal_svr_252d"] = ratio - prior_median_252
    for window in (5, 10, 20):
        group[f"casv_{window}d"] = group["abnormal_svr_60d"].rolling(window, min_periods=max(2, window // 2)).sum()

    group["svr_acceleration_5_20"] = group["svr_5d"] - group["svr_20d"]
    group["svr_acceleration_20_60"] = group["svr_20d"] - group["svr_60d"]
    conditions = {
        "median": ratio.gt(prior_median_252).where(prior_median_252.notna()),
        "75p": ratio.gt(prior_75).where(prior_75.notna()),
        "90p": ratio.gt(prior_90).where(prior_90.notna()),
    }
    for label, condition in conditions.items():
        numeric = condition.astype(float)
        for window in (20, 60):
            group[f"persistence_above_{label}_{window}d"] = numeric.rolling(
                window, min_periods=max(5, window // 2)
            ).mean()

    casv_percentile = _rolling_percentile(group["casv_20d"], history, minimum)
    acceleration_percentile = _rolling_percentile(group["svr_acceleration_5_20"], history, minimum)
    weights = config["activity"]["score_weights"]
    activity = 100 * (
        group["svr_pctile_5d_1y"].fillna(0.5) * float(weights["svr_5d_percentile"])
        + group["svr_pctile_20d_1y"].fillna(0.5) * float(weights["svr_20d_percentile"])
        + group["svr_pctile_60d_1y"].fillna(0.5) * float(weights["svr_60d_percentile"])
        + group["persistence_above_75p_20d"].fillna(0.0) * float(weights["persistence_above_75p_20d"])
        + casv_percentile.fillna(0.5) * float(weights["casv_20d_percentile"])
        + acceleration_percentile.fillna(0.5) * float(weights["svr_acceleration_percentile"])
    )
    group["short_activity_score"] = activity.clip(0, 100)
    group["data_quality_status"] = group.get("data_quality_status", "valid").fillna("valid")
    return group


def compute_daily_short_volume_features(rows: pd.DataFrame | list[dict], config: dict | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(rows).copy()
    if frame.empty:
        return pd.DataFrame(columns=DAILY_FEATURE_COLUMNS)
    required = {"trade_date", "ticker", "short_volume", "short_exempt_volume", "total_volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing daily FINRA fields: {sorted(missing)}")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    for column in ("short_volume", "short_exempt_volume", "total_volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["short_volume_ratio"] = _safe_divide(frame["short_volume"], frame["total_volume"])
    frame = frame.dropna(subset=["trade_date", "ticker", "short_volume_ratio"])
    frame = frame.sort_values(["ticker", "trade_date"]).drop_duplicates(["ticker", "trade_date"], keep="last")
    cfg = config or load_short_analytics_config()
    featured = frame.groupby("ticker", group_keys=False, sort=False).apply(
        _ticker_daily_features, config=cfg, include_groups=False
    )
    # pandas 2.2 excludes grouping columns with include_groups=False.
    if "ticker" not in featured.columns:
        featured = featured.reset_index(level=0)
    featured["trade_date"] = pd.to_datetime(featured["trade_date"]).dt.date
    return featured[DAILY_FEATURE_COLUMNS].replace({np.nan: None})


def fetch_daily_short_volume_history(
    engine,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    tickers: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["total_volume > 0"]
    params: dict[str, object] = {}
    if start_date:
        clauses.append("trade_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        clauses.append("trade_date <= :end_date")
        params["end_date"] = end_date
    if tickers:
        clauses.append("ticker IN :tickers")
        params["tickers"] = tickers
    query = text(
        f"""
        SELECT trade_date, ticker, short_volume, short_exempt_volume, total_volume,
               short_volume_ratio, data_quality_status
        FROM public.finra_short_volume
        WHERE {' AND '.join(clauses)}
          AND raw_symbol IS NOT NULL
          AND normalized_ticker IS NOT NULL
          AND normalized_ticker = ticker
        ORDER BY ticker, trade_date
        """
    )
    if tickers:
        query = query.bindparams(bindparam("tickers", expanding=True))
    return pd.read_sql(query, engine, params=params)


def upsert_daily_short_volume_features(engine, features: pd.DataFrame, *, chunk_size: int = 10000) -> int:
    if features.empty:
        return 0
    setup_short_analytics_schema(engine)
    columns = DAILY_FEATURE_COLUMNS
    query = text(
        f"""
        INSERT INTO public.finra_short_volume_daily_features ({', '.join(columns)}, calculated_at)
        VALUES ({', '.join(':' + item for item in columns)}, now())
        ON CONFLICT (trade_date, ticker) DO UPDATE SET
        {', '.join(f'{item} = EXCLUDED.{item}' for item in columns if item not in {'trade_date', 'ticker'})},
        calculated_at = now()
        """
    )
    rows = features.replace({np.nan: None}).to_dict(orient="records")
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(query, rows[start : start + chunk_size])
    return len(rows)


def refresh_daily_short_volume_features(
    engine,
    *,
    output_start_date: date | None = None,
    end_date: date | None = None,
    batch_size: int = 500,
    progress=print,
) -> dict:
    setup_short_analytics_schema(engine)
    # Keep enough warm-up history for rolling 60D measures normalized over 252 observations.
    load_start = output_start_date - timedelta(days=550) if output_start_date else None
    with engine.connect() as conn:
        tickers = [
            row[0]
            for row in conn.execute(
                text(
                    """
                    SELECT DISTINCT ticker
                    FROM public.finra_short_volume
                    WHERE raw_symbol IS NOT NULL
                      AND normalized_ticker IS NOT NULL
                      AND normalized_ticker = ticker
                    ORDER BY ticker
                    """
                )
            )
        ]
    raw_count = 0
    feature_count = 0
    upserted = 0
    minimum_date = None
    maximum_date = None
    for start in range(0, len(tickers), batch_size):
        batch = tickers[start : start + batch_size]
        raw = fetch_daily_short_volume_history(engine, start_date=load_start, end_date=end_date, tickers=batch)
        raw_count += len(raw)
        if progress:
            progress(f"FINRA daily analytics batch={start // batch_size + 1} tickers={len(batch)} raw_rows={len(raw):,}")
        features = compute_daily_short_volume_features(raw)
        if output_start_date:
            features = features[pd.to_datetime(features["trade_date"]) >= pd.Timestamp(output_start_date)]
        if features.empty:
            continue
        feature_count += len(features)
        upserted += upsert_daily_short_volume_features(engine, features)
        batch_minimum = min(features["trade_date"])
        batch_maximum = max(features["trade_date"])
        minimum_date = batch_minimum if minimum_date is None else min(minimum_date, batch_minimum)
        maximum_date = batch_maximum if maximum_date is None else max(maximum_date, batch_maximum)
    if minimum_date is not None and maximum_date is not None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE public.finra_short_volume raw
                    SET short_volume_z_60d = features.svr_z60,
                        short_pressure_flag = COALESCE(features.svr_z60 >= 2, false),
                        short_covering_candidate = COALESCE(features.svr_z60 >= 2 AND prices.pct_chg > 0, false),
                        bearish_pressure_flag = COALESCE(features.svr_z60 >= 2 AND prices.pct_chg < 0, false),
                        updated_at = now()
                    FROM public.finra_short_volume_daily_features features
                    LEFT JOIN public.us_equities prices
                      ON prices.date = features.trade_date AND prices.ticker = features.ticker
                    WHERE raw.trade_date = features.trade_date
                      AND raw.ticker = features.ticker
                      AND raw.trade_date BETWEEN :minimum_date AND :maximum_date
                    """
                ),
                {"minimum_date": minimum_date, "maximum_date": maximum_date},
            )
    if progress:
        progress(f"FINRA daily analytics upserted rows={upserted:,}")
    return {"raw_rows": raw_count, "feature_rows": feature_count, "upserted": upserted, "ticker_count": len(tickers)}


def _score(value: object, default: float = 50.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(number) else min(max(number, 0.0), 100.0)


def reason_json(**components) -> dict:
    return {key: value for key, value in components.items() if value is not None}


def serialize_reason(value: dict) -> str:
    return json.dumps(value, sort_keys=True, default=str)
