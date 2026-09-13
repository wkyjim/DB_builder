"""Batched orchestration and persistence for FINRA short analytics."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text

from db_builder.finra_short_analytics import setup_short_analytics_schema
from db_builder.short_regime import (
    build_short_interest_intervals,
    compute_price_relative_features,
    compute_short_pressure_effectiveness,
    merge_short_interest_point_in_time,
    score_and_classify,
    validate_point_in_time,
)


ANALYTICS_COLUMNS = [
    "ticker", "analytics_date", "short_volume", "short_exempt_volume", "total_finra_volume",
    "short_volume_ratio", "short_exempt_ratio", "svr_5d", "svr_20d", "svr_60d", "svr_z20",
    "svr_z60", "svr_pctile_5d", "svr_pctile_20d", "svr_pctile_60d", "casv_5d", "casv_10d",
    "casv_20d", "svr_acceleration_5_20", "svr_acceleration_20_60", "si_settlement_date",
    "persistence_above_median_20d", "persistence_above_median_60d",
    "persistence_above_75p_20d", "persistence_above_75p_60d",
    "persistence_above_90p_20d", "persistence_above_90p_60d",
    "si_publication_date", "short_interest", "short_pct_float", "days_to_cover", "si_observation_count", "si_change_1obs",
    "si_change_2obs", "si_change_4obs", "si_change_8obs", "si_change_12obs", "si_slope_6m",
    "si_slope_12m", "si_r2_6m", "si_r2_12m", "si_acceleration", "si_persistence_6m",
    "si_persistence_12m", "si_persistence_24m", "si_volatility_6m", "si_volatility_12m",
    "own_si_percentile_1y", "own_si_percentile_3y", "own_si_percentile_5y", "market_si_percentile", "sector_si_percentile",
    "industry_si_percentile", "close", "daily_return", "volume", "dollar_adv20", "dollar_adv60",
    "volatility_20d", "volatility_60d", "rel_return_1m", "rel_return_3m", "rel_return_6m",
    "rel_return_12m", "up_capture", "down_capture", "long_short_asymmetry",
    "short_pressure_effectiveness", "ma20", "ma50", "ma100", "ma200", "ma20_slope",
    "ma50_slope", "ma100_slope", "ma200_slope", "rsi14", "macd", "macd_signal",
    "macd_histogram", "short_position_score", "short_activity_score",
    "short_flow_confirmation_score", "funding_short_score", "funding_short_quality_score", "unwind_risk_score", "short_regime",
    "regime_confidence", "regime_reason_json", "data_quality_status",
]


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _query_for_tickers(engine, sql: str, tickers: list[str], **params) -> pd.DataFrame:
    query = text(sql).bindparams(bindparam("tickers", expanding=True))
    return pd.read_sql(query, engine, params={"tickers": tickers, **params})


def fetch_analytics_tickers(engine) -> list[str]:
    with engine.connect() as conn:
        return [
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


def fetch_classifications(engine, tickers: list[str]) -> pd.DataFrame:
    try:
        return _query_for_tickers(
            engine,
            "SELECT ticker, sector, industry FROM public.security_classification WHERE ticker IN :tickers",
            tickers,
        )
    except Exception:
        return pd.DataFrame(columns=["ticker", "sector", "industry"])


def fetch_daily_feature_batch(engine, tickers: list[str], start_date: date, end_date: date) -> pd.DataFrame:
    return _query_for_tickers(
        engine,
        """
        SELECT raw.trade_date, raw.ticker, raw.short_volume, raw.short_exempt_volume,
               raw.total_volume, raw.data_quality_status,
               features.short_volume_ratio, features.short_exempt_ratio,
               features.short_exempt_share_of_short, features.svr_5d, features.svr_20d,
               features.svr_60d, features.svr_z20, features.svr_z60,
               features.svr_pctile_5d_1y, features.svr_pctile_20d_1y,
               features.svr_pctile_60d_1y, features.abnormal_svr_60d,
               features.casv_5d, features.casv_10d, features.casv_20d,
               features.svr_acceleration_5_20, features.svr_acceleration_20_60,
               features.persistence_above_median_20d, features.persistence_above_median_60d,
               features.persistence_above_75p_20d, features.persistence_above_75p_60d,
               features.persistence_above_90p_20d, features.persistence_above_90p_60d,
               features.short_activity_score
        FROM public.finra_short_volume raw
        JOIN public.finra_short_volume_daily_features features
          ON features.trade_date = raw.trade_date AND features.ticker = raw.ticker
        WHERE raw.ticker IN :tickers AND raw.trade_date BETWEEN :start_date AND :end_date
        ORDER BY raw.ticker, raw.trade_date
        """,
        tickers,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_price_batch(engine, tickers: list[str], start_date: date, end_date: date) -> pd.DataFrame:
    requested = sorted(set(tickers) | {"SPY"})
    return _query_for_tickers(
        engine,
        """
        SELECT prices.date, prices.ticker, prices.close, prices.volume,
               indicators.ma_20, indicators.ma_50, indicators.ma_100, indicators.ma_200,
               indicators.rsi_14, indicators.macd, indicators.macd_signal,
               indicators.macd_hist, indicators.volatility_20d
        FROM public.us_equities prices
        LEFT JOIN public.us_equities_indicators indicators
          ON indicators.date = prices.date AND indicators.ticker = prices.ticker
        WHERE prices.ticker IN :tickers AND prices.date BETWEEN :start_date AND :end_date
        ORDER BY prices.ticker, prices.date
        """,
        requested,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_si_feature_batch(engine, tickers: list[str]) -> pd.DataFrame:
    return _query_for_tickers(
        engine,
        """
        SELECT * FROM public.finra_short_interest_features
        WHERE ticker IN :tickers ORDER BY ticker, settlement_date
        """,
        tickers,
    )


def _prepare_final_rows(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    mappings = {
        "total_volume": "total_finra_volume",
        "svr_pctile_5d_1y": "svr_pctile_5d",
        "svr_pctile_20d_1y": "svr_pctile_20d",
        "svr_pctile_60d_1y": "svr_pctile_60d",
        "settlement_date": "si_settlement_date",
        "publication_date": "si_publication_date",
        "ma_20": "ma20",
        "ma_50": "ma50",
        "ma_100": "ma100",
        "ma_200": "ma200",
        "rsi_14": "rsi14",
        "macd_hist": "macd_histogram",
    }
    output = output.rename(columns=mappings)
    output["short_pct_float"] = None
    for column in ANALYTICS_COLUMNS:
        if column not in output.columns:
            output[column] = None
    for column in ("analytics_date", "si_settlement_date", "si_publication_date"):
        output[column] = pd.to_datetime(output[column], errors="coerce").dt.date
    output["regime_reason_json"] = output["regime_reason_json"].map(
        lambda value: json.dumps(value, sort_keys=True, default=str) if isinstance(value, dict) else value
    )
    return output[ANALYTICS_COLUMNS].replace({np.nan: None})


def upsert_short_analytics(engine, frame: pd.DataFrame, *, chunk_size: int = 5000) -> int:
    if frame.empty:
        return 0
    setup_short_analytics_schema(engine)
    query = text(
        f"""
        INSERT INTO public.us_equities_short_analytics (
            {', '.join(ANALYTICS_COLUMNS)}, calculated_at
        ) VALUES (
            {', '.join('CAST(:regime_reason_json AS jsonb)' if item == 'regime_reason_json' else ':' + item for item in ANALYTICS_COLUMNS)}, now()
        )
        ON CONFLICT (ticker, analytics_date) DO UPDATE SET
            {', '.join(f'{item} = EXCLUDED.{item}' for item in ANALYTICS_COLUMNS if item not in {'ticker', 'analytics_date'})},
            calculated_at = now()
        """
    )
    rows = frame.to_dict(orient="records")
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(query, rows[start : start + chunk_size])
    return len(rows)


INTERVAL_COLUMNS = [
    "ticker", "start_settlement_date", "end_settlement_date", "end_publication_date",
    "daily_observation_count", "mean_svr", "median_svr", "max_svr", "mean_abnormal_svr",
    "interval_casv", "fraction_above_75p", "fraction_above_90p", "svr_acceleration",
    "stock_return", "benchmark_return", "residual_return", "volume_change", "realized_volatility",
    "actual_si_change", "expected_si_direction", "p_increase", "p_flat", "p_decrease",
    "flow_confirmation_signal", "signal_confidence", "component_json",
]


def upsert_short_interest_intervals(engine, frame: pd.DataFrame, *, chunk_size: int = 5000) -> int:
    if frame.empty:
        return 0
    prepared = frame.reindex(columns=INTERVAL_COLUMNS).replace({np.nan: None})
    query = text(
        f"""
        INSERT INTO public.finra_short_interest_intervals ({', '.join(INTERVAL_COLUMNS)}, calculated_at)
        VALUES ({', '.join('CAST(:component_json AS jsonb)' if item == 'component_json' else ':' + item for item in INTERVAL_COLUMNS)}, now())
        ON CONFLICT (ticker, start_settlement_date, end_settlement_date) DO UPDATE SET
            {', '.join(f'{item} = EXCLUDED.{item}' for item in INTERVAL_COLUMNS if item not in {'ticker', 'start_settlement_date', 'end_settlement_date'})},
            calculated_at = now()
        """
    )
    rows = prepared.to_dict(orient="records")
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(query, rows[start : start + chunk_size])
    return len(rows)


def refresh_short_analytics(
    engine,
    *,
    start_date: date,
    end_date: date,
    batch_size: int = 200,
    build_intervals: bool = True,
    start_after_ticker: str | None = None,
    progress=print,
) -> dict:
    setup_short_analytics_schema(engine)
    tickers = fetch_analytics_tickers(engine)
    if start_after_ticker:
        tickers = [ticker for ticker in tickers if ticker > start_after_ticker]
    warmup_date = start_date - timedelta(days=550)
    total_analytics = 0
    total_intervals = 0
    validation_errors: list[str] = []
    for batch_number, batch in enumerate(_chunks(tickers, batch_size), start=1):
        if progress:
            progress(f"FINRA final analytics batch={batch_number} tickers={len(batch)} first={batch[0]} last={batch[-1]}")
        daily = fetch_daily_feature_batch(engine, batch, warmup_date, end_date)
        if daily.empty:
            continue
        classifications = fetch_classifications(engine, batch)
        prices = fetch_price_batch(engine, batch, warmup_date - timedelta(days=370), end_date)
        price_features = compute_price_relative_features(prices, classifications)
        price_features = price_features[price_features["ticker"].isin(batch)]
        daily_for_merge = daily.rename(columns={"trade_date": "analytics_date"}).copy()
        daily_for_merge["analytics_date"] = pd.to_datetime(daily_for_merge["analytics_date"], errors="coerce")
        price_features["analytics_date"] = pd.to_datetime(price_features["analytics_date"], errors="coerce")
        merged = daily_for_merge.merge(
            price_features,
            on=["ticker", "analytics_date"],
            how="left",
            suffixes=("", "_price"),
        )
        merged = compute_short_pressure_effectiveness(merged)
        scoring_input = merged[
            pd.to_datetime(merged["analytics_date"], errors="coerce").dt.date >= start_date
        ].copy()
        si_features = fetch_si_feature_batch(engine, batch)
        if not si_features.empty:
            scoring_input = merge_short_interest_point_in_time(scoring_input, si_features)
        scored = score_and_classify(scoring_input)
        validation_errors.extend(validate_point_in_time(scored))
        prepared = _prepare_final_rows(scored)
        total_analytics += upsert_short_analytics(engine, prepared)
        if build_intervals and not si_features.empty:
            interval_prices = price_features.rename(columns={"analytics_date": "trade_date"})
            intervals = build_short_interest_intervals(si_features, daily, interval_prices)
            total_intervals += upsert_short_interest_intervals(engine, intervals)
    if validation_errors:
        raise ValueError("; ".join(sorted(set(validation_errors))))
    return {
        "ticker_count": len(tickers),
        "analytics_upserted": total_analytics,
        "intervals_upserted": total_intervals,
        "validation_errors": validation_errors,
    }


def latest_short_analytics(engine, *, limit: int = 50) -> pd.DataFrame:
    query = text(
        """
        SELECT ticker, analytics_date, short_regime, regime_confidence,
               funding_short_score, short_position_score, short_activity_score,
               unwind_risk_score, short_interest, days_to_cover,
               rel_return_3m, rel_return_6m, rsi14, regime_reason_json
        FROM public.us_equities_short_analytics
        WHERE analytics_date = (SELECT MAX(analytics_date) FROM public.us_equities_short_analytics)
        ORDER BY GREATEST(funding_short_score, unwind_risk_score, short_activity_score) DESC NULLS LAST
        LIMIT :limit
        """
    )
    return pd.read_sql(query, engine, params={"limit": limit})
