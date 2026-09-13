"""Point-in-time FINRA short positioning, effectiveness, and regime engine."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text

from db_builder.finra_short_analytics import setup_short_analytics_schema
from db_builder.short_analytics_config import load_short_analytics_config


def clamp(value: object, lower: float = 0.0, upper: float = 100.0, default: float = 50.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(result) else min(max(result, lower), upper)


def _as_float(value: object, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(result) else result


def _score_from_percentile(value: object) -> float:
    number = _as_float(value)
    return 50.0 if number is None else clamp(number * 100)


def _direction_probabilities(signal: float) -> tuple[float, float, float]:
    # Transparent softmax centered on a directional score in [-1, 1].
    logits = np.array([signal * 2.0, (1.0 - abs(signal)) * 1.5, -signal * 2.0])
    probabilities = np.exp(logits - logits.max())
    probabilities /= probabilities.sum()
    return tuple(float(item) for item in probabilities)


def expected_si_direction(row: dict, config: dict | None = None) -> dict:
    cfg = config or load_short_analytics_config()
    abnormal = _as_float(row.get("mean_abnormal_svr"), 0.0) or 0.0
    fraction_75 = _as_float(row.get("fraction_above_75p"), 0.0) or 0.0
    acceleration = _as_float(row.get("svr_acceleration"), 0.0) or 0.0
    residual = _as_float(row.get("residual_return"), 0.0) or 0.0
    signal = float(np.tanh(abnormal * 8 + (fraction_75 - 0.25) * 1.5 + acceleration * 5 - residual * 2))
    p_increase, p_flat, p_decrease = _direction_probabilities(signal)
    if p_increase == max(p_increase, p_flat, p_decrease):
        label = "INCREASE"
    elif p_decrease == max(p_increase, p_flat, p_decrease):
        label = "DECREASE"
    else:
        label = "FLAT"
    return {"expected_si_direction": label, "p_increase": p_increase, "p_flat": p_flat, "p_decrease": p_decrease}


def short_flow_confirmation(actual_change: object, activity_score: object, residual_return: object, threshold: float = 0.03) -> tuple[str, float]:
    change = _as_float(actual_change)
    activity = _as_float(activity_score, 50.0) or 50.0
    residual = _as_float(residual_return, 0.0) or 0.0
    if change is None:
        return "AWAITING_POSITION_CONFIRMATION", 25.0
    if change > threshold and activity >= 65:
        return "CONFIRMED_SHORT_ACCUMULATION", clamp(65 + min(change * 200, 20) + max(-residual * 100, 0))
    if change > threshold:
        return "STRUCTURAL_ACCUMULATION", clamp(60 + min(change * 150, 20))
    if abs(change) <= threshold and activity >= 70:
        return "HIGH_SHORT_TURNOVER_NO_BUILD", clamp(60 + (activity - 70) * 0.5)
    if change < -threshold and activity >= 65:
        return "HIGH_SHORT_ACTIVITY_WHILE_SI_FALLS", clamp(60 + min(abs(change) * 150, 20))
    if change < -threshold:
        return "SHORT_COVERING", clamp(60 + min(abs(change) * 150, 25))
    return "STABLE_STRUCTURAL_SHORT", 55.0


def _compound_return(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(np.expm1(np.log1p(clean.clip(lower=-0.999)).sum()))


def build_short_interest_intervals(
    short_interest_features: pd.DataFrame,
    daily_features: pd.DataFrame,
    prices: pd.DataFrame | None = None,
    *,
    config: dict | None = None,
) -> pd.DataFrame:
    if short_interest_features.empty or daily_features.empty:
        return pd.DataFrame()
    cfg = config or load_short_analytics_config()
    increase_threshold = float(cfg["short_interest_direction_thresholds"]["increase"])
    si = short_interest_features.copy()
    si["settlement_date"] = pd.to_datetime(si["settlement_date"])
    si["publication_date"] = pd.to_datetime(si["publication_date"])
    daily = daily_features.copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    if prices is not None and not prices.empty:
        price = prices.copy()
        price["trade_date"] = pd.to_datetime(price.get("trade_date", price.get("date")))
        keep = [item for item in ("ticker", "trade_date", "daily_return", "benchmark_return", "volume", "volatility_20d") if item in price.columns]
        daily = daily.merge(price[keep], on=["ticker", "trade_date"], how="left")
    results: list[dict] = []
    daily_groups = {ticker: group.sort_values("trade_date") for ticker, group in daily.groupby("ticker", sort=False)}
    for ticker, observations in si.groupby("ticker", sort=False):
        observations = observations.sort_values("settlement_date").reset_index(drop=True)
        ticker_daily = daily_groups.get(ticker)
        if ticker_daily is None or len(observations) < 2:
            continue
        for index in range(1, len(observations)):
            start = observations.iloc[index - 1]
            end = observations.iloc[index]
            interval = ticker_daily[
                ticker_daily["trade_date"].gt(start["settlement_date"])
                & ticker_daily["trade_date"].le(end["settlement_date"])
            ]
            if interval.empty:
                continue
            actual_change = _as_float(end.get("si_change_1obs"))
            stock_return = _compound_return(interval.get("daily_return", pd.Series(dtype=float)))
            benchmark_return = _compound_return(interval.get("benchmark_return", pd.Series(dtype=float)))
            residual_return = None if stock_return is None or benchmark_return is None else stock_return - benchmark_return
            row = {
                "ticker": ticker,
                "start_settlement_date": start["settlement_date"].date(),
                "end_settlement_date": end["settlement_date"].date(),
                "end_publication_date": end["publication_date"].date() if pd.notna(end["publication_date"]) else None,
                "daily_observation_count": len(interval),
                "mean_svr": pd.to_numeric(interval["short_volume_ratio"], errors="coerce").mean(),
                "median_svr": pd.to_numeric(interval["short_volume_ratio"], errors="coerce").median(),
                "max_svr": pd.to_numeric(interval["short_volume_ratio"], errors="coerce").max(),
                "mean_abnormal_svr": pd.to_numeric(interval["abnormal_svr_60d"], errors="coerce").mean(),
                "interval_casv": pd.to_numeric(interval["abnormal_svr_60d"], errors="coerce").sum(min_count=1),
                "fraction_above_75p": pd.to_numeric(interval["svr_pctile_20d_1y"], errors="coerce").gt(0.75).mean(),
                "fraction_above_90p": pd.to_numeric(interval["svr_pctile_20d_1y"], errors="coerce").gt(0.90).mean(),
                "svr_acceleration": pd.to_numeric(interval["svr_acceleration_5_20"], errors="coerce").mean(),
                "stock_return": stock_return,
                "benchmark_return": benchmark_return,
                "residual_return": residual_return,
                "volume_change": None,
                "realized_volatility": pd.to_numeric(interval.get("daily_return"), errors="coerce").std() if "daily_return" in interval else None,
                "actual_si_change": actual_change,
            }
            if "volume" in interval and len(interval) > 1:
                first = _as_float(interval["volume"].iloc[0])
                last = _as_float(interval["volume"].iloc[-1])
                row["volume_change"] = None if not first or last is None else last / first - 1
            row.update(expected_si_direction(row, cfg))
            activity_score = pd.to_numeric(interval["short_activity_score"], errors="coerce").mean()
            signal, confidence = short_flow_confirmation(actual_change, activity_score, residual_return, increase_threshold)
            row["flow_confirmation_signal"] = signal
            row["signal_confidence"] = confidence
            row["component_json"] = json.dumps(
                {
                    "mean_short_activity_score": None if pd.isna(activity_score) else float(activity_score),
                    "daily_observation_count": len(interval),
                    "actual_si_change": actual_change,
                    "publication_gated": True,
                },
                sort_keys=True,
            )
            results.append(row)
    return pd.DataFrame(results).replace({np.nan: None})


def _price_ticker_features(group: pd.DataFrame) -> pd.DataFrame:
    ticker = group.name
    group = group.sort_values("analytics_date").copy()
    group["ticker"] = ticker
    close = pd.to_numeric(group["close"], errors="coerce")
    volume = pd.to_numeric(group["volume"], errors="coerce")
    group["daily_return"] = close.pct_change(fill_method=None)
    group["dollar_volume"] = close * volume
    group["dollar_adv20"] = group["dollar_volume"].rolling(20, min_periods=10).mean()
    group["dollar_adv60"] = group["dollar_volume"].rolling(60, min_periods=30).mean()
    group["volatility_60d"] = group["daily_return"].rolling(60, min_periods=30).std() * np.sqrt(252)
    for days, label in ((20, "1m"), (60, "3m"), (120, "6m"), (252, "12m")):
        stock = np.log1p(group["daily_return"].clip(lower=-0.999)).rolling(days, min_periods=max(10, days // 2)).sum()
        benchmark = np.log1p(pd.to_numeric(group["benchmark_return"], errors="coerce").clip(lower=-0.999)).rolling(
            days, min_periods=max(10, days // 2)
        ).sum()
        group[f"rel_return_{label}"] = np.expm1(stock) - np.expm1(benchmark)
    benchmark = pd.to_numeric(group["benchmark_return"], errors="coerce")
    up_stock = group["daily_return"].where(benchmark > 0)
    up_bench = benchmark.where(benchmark > 0)
    down_stock = group["daily_return"].where(benchmark < 0)
    down_bench = benchmark.where(benchmark < 0)
    group["up_capture"] = up_stock.rolling(120, min_periods=30).sum() / up_bench.rolling(120, min_periods=30).sum().replace(0, np.nan)
    group["down_capture"] = down_stock.rolling(120, min_periods=30).sum() / down_bench.rolling(120, min_periods=30).sum().replace(0, np.nan)
    group["long_short_asymmetry"] = group["down_capture"] - group["up_capture"]
    for ma in (20, 50, 100, 200):
        column = f"ma_{ma}"
        group[f"ma{ma}_slope"] = pd.to_numeric(group[column], errors="coerce").pct_change(5, fill_method=None) / 5
    group["macd_hist_change"] = pd.to_numeric(group["macd_hist"], errors="coerce").diff()
    group["rsi_change_5d"] = pd.to_numeric(group["rsi_14"], errors="coerce").diff(5)
    return group


def compute_price_relative_features(prices: pd.DataFrame, classifications: pd.DataFrame | None = None) -> pd.DataFrame:
    frame = prices.copy()
    if frame.empty:
        return frame
    frame["analytics_date"] = pd.to_datetime(frame.get("analytics_date", frame.get("date")))
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    raw_return = frame.sort_values(["ticker", "analytics_date"]).groupby("ticker")["close"].pct_change(fill_method=None)
    frame["_raw_return"] = raw_return
    if classifications is not None and not classifications.empty:
        classes = classifications[[item for item in ("ticker", "sector", "industry") if item in classifications.columns]].drop_duplicates("ticker")
        frame = frame.merge(classes, on="ticker", how="left")
    spy = frame.loc[frame["ticker"].eq("SPY"), ["analytics_date", "_raw_return"]].drop_duplicates("analytics_date").rename(columns={"_raw_return": "_spy_return"})
    frame = frame.merge(spy, on="analytics_date", how="left")
    # Relative and capture statistics use one stable broad-market benchmark.
    # A batch-local industry average can contain only the subject ticker,
    # which silently produces 0% relative returns and 1.0 capture ratios.
    frame["benchmark_return"] = frame["_spy_return"]
    featured = frame.sort_values(["ticker", "analytics_date"]).groupby("ticker", group_keys=False, sort=False).apply(
        _price_ticker_features, include_groups=False
    )
    if "ticker" not in featured.columns:
        featured = featured.reset_index(level=0)
    return featured.drop(columns=["_raw_return", "_spy_return"], errors="ignore")


def compute_short_pressure_effectiveness(frame: pd.DataFrame, *, high_percentile: float = 0.8) -> pd.DataFrame:
    result = frame.sort_values(["ticker", "analytics_date"]).copy()
    result["residual_return"] = pd.to_numeric(result["daily_return"], errors="coerce") - pd.to_numeric(result["benchmark_return"], errors="coerce")
    result["_high_short"] = pd.to_numeric(result["svr_pctile_20d_1y"], errors="coerce").ge(high_percentile)

    def ticker_effectiveness(group: pd.DataFrame) -> pd.DataFrame:
        ticker = group.name
        group = group.copy()
        group["ticker"] = ticker
        high = group["_high_short"]
        same_day = group["residual_return"].where(high)
        # Future outcomes become available only after the relevant horizon.
        future_1 = group["residual_return"].shift(-1).where(high).shift(1)
        future_3 = sum(group["residual_return"].shift(-offset) for offset in (1, 2, 3)).where(high).shift(3)
        future_5 = sum(group["residual_return"].shift(-offset) for offset in (1, 2, 3, 4, 5)).where(high).shift(5)
        for label, values in (("same", same_day), ("next_1d", future_1), ("next_3d", future_3), ("next_5d", future_5)):
            group[f"spe_{label}_mean_60d"] = values.rolling(60, min_periods=5).mean()
        negative_rate = same_day.lt(0).where(same_day.notna()).astype(float).rolling(60, min_periods=5).mean()
        mean_outcome = pd.concat([same_day, future_1, future_3 / 3, future_5 / 5], axis=1).mean(axis=1).rolling(60, min_periods=5).mean()
        observation_count = high.astype(int).rolling(60, min_periods=1).sum()
        score = (negative_rate * 60 + (20 - mean_outcome * 2000).clip(0, 40)).clip(0, 100)
        group["short_pressure_effectiveness"] = score.where(observation_count >= 5)
        return group

    output = result.groupby("ticker", group_keys=False, sort=False).apply(ticker_effectiveness, include_groups=False)
    if "ticker" not in output.columns:
        output = output.reset_index(level=0)
    return output.drop(columns=["_high_short"], errors="ignore")


def merge_short_interest_point_in_time(daily: pd.DataFrame, si_features: pd.DataFrame) -> pd.DataFrame:
    if daily.empty or si_features.empty:
        return daily.copy()
    left = daily.copy()
    right = si_features.copy()
    left["analytics_date"] = pd.to_datetime(left["analytics_date"])
    right["publication_date"] = pd.to_datetime(right["publication_date"])
    right = right[right["publication_date"].notna()].copy()
    left = left.sort_values(["analytics_date", "ticker"])
    right = right.sort_values(["publication_date", "ticker"])
    return pd.merge_asof(
        left,
        right,
        left_on="analytics_date",
        right_on="publication_date",
        by="ticker",
        direction="backward",
        allow_exact_matches=True,
        suffixes=("", "_si"),
    )


def _technical_compatibility(row: pd.Series) -> float:
    close = _as_float(row.get("close"))
    values = [_as_float(row.get(f"ma_{window}")) for window in (20, 50, 100, 200)]
    below = sum(close is not None and value is not None and close < value for value in values)
    score = below / 4 * 70
    rsi = _as_float(row.get("rsi_14"), 50) or 50
    if 30 <= rsi < 45:
        score += 20
    elif rsi < 30:
        score += 5  # oversold increases squeeze risk, not structural conviction
    macd_hist = _as_float(row.get("macd_hist"), 0) or 0
    score += 10 if macd_hist < 0 else 0
    return clamp(score)


def score_short_row(row: pd.Series, config: dict | None = None) -> dict:
    cfg = config or load_short_analytics_config()
    own_percentile = _score_from_percentile(row.get("own_si_percentile_3y"))
    industry_percentile = _score_from_percentile(row.get("industry_si_percentile"))
    persistence = clamp((_as_float(row.get("si_persistence_12m"), 0.5) or 0.5) * 100)
    dtc = _as_float(row.get("days_to_cover"))
    dtc_score = 50 if dtc is None else clamp(dtc / 10 * 100)
    trend_quality = clamp(row.get("si_trend_quality"))
    slope = _as_float(row.get("si_slope_12m"), 0) or 0
    trend_direction = 100 if slope > 0 else 25 if slope < 0 else 50
    stability = clamp(100 - (_as_float(row.get("si_volatility_12m"), 0.25) or 0.25) * 200)
    position_score = clamp(own_percentile * 0.25 + industry_percentile * 0.20 + persistence * 0.20 + dtc_score * 0.15 + trend_quality * 0.10 + stability * 0.10)
    activity_score = clamp(row.get("short_activity_score"))
    pressure = clamp(row.get("short_pressure_effectiveness"))
    relative_values = [_as_float(row.get(item)) for item in ("rel_return_3m", "rel_return_6m", "rel_return_12m")]
    valid_relative = [item for item in relative_values if item is not None]
    relative_weakness = 50 if not valid_relative else clamp(50 - np.mean(valid_relative) * 250)
    liquidity = _as_float(row.get("dollar_adv20"))
    liquidity_score = 50 if not liquidity or liquidity <= 0 else clamp((math.log10(liquidity) - 5) / 4 * 100)
    asymmetry = _as_float(row.get("long_short_asymmetry"))
    asymmetry_score = 50 if asymmetry is None else clamp(50 + asymmetry * 40)
    technical = _technical_compatibility(row)
    weights = cfg.get("scores", {}).get("funding_short", {})
    components = {
        "short_interest_persistence": persistence,
        "industry_relative_weakness": relative_weakness,
        "short_interest_trend": clamp(trend_quality * 0.7 + trend_direction * 0.3),
        "short_interest_stability": stability,
        "institutional_liquidity": liquidity_score,
        "capture_asymmetry": asymmetry_score,
        "pressure_effectiveness": pressure,
        "daily_short_activity": activity_score,
        "technical_compatibility": technical,
    }
    funding_score = clamp(sum(components[key] * float(weights.get(key, 0)) for key in components))
    dtc_quality = 50 if dtc is None else clamp(100 - abs(dtc - 3.0) * 10)
    realized_volatility = _as_float(row.get("volatility_20d"))
    if realized_volatility is not None and realized_volatility > 3:
        realized_volatility /= 100.0
    volatility_quality = 50 if realized_volatility is None else clamp(100 - abs(realized_volatility - 0.30) * 140)
    rsi = _as_float(row.get("rsi_14"), 50) or 50
    squeeze_resilience = clamp(100 - max((30 - rsi) * 4, 0) - max(dtc - 5, 0) * 6) if dtc is not None else clamp(100 - max((30 - rsi) * 4, 0))
    comparability = 100.0 if row.get("industry") or row.get("sector") else 35.0
    funding_quality_components = {
        "liquidity": liquidity_score,
        "days_to_cover_quality": dtc_quality,
        "relative_weakness": relative_weakness,
        "short_interest_stability": stability,
        "moderate_volatility": volatility_quality,
        "lower_squeeze_risk": squeeze_resilience,
        "industry_comparability": comparability,
    }
    funding_quality_score = clamp(
        funding_quality_components["liquidity"] * 0.25
        + funding_quality_components["days_to_cover_quality"] * 0.15
        + funding_quality_components["relative_weakness"] * 0.20
        + funding_quality_components["short_interest_stability"] * 0.15
        + funding_quality_components["moderate_volatility"] * 0.10
        + funding_quality_components["lower_squeeze_risk"] * 0.10
        + funding_quality_components["industry_comparability"] * 0.05
    )
    si_change = _as_float(row.get("si_change_1obs"))
    flow_signal, flow_confidence = short_flow_confirmation(si_change, activity_score, row.get("rel_return_1m"), float(cfg["short_interest_direction_thresholds"]["increase"]))
    relative_reversal = clamp(50 + (_as_float(row.get("rel_return_1m"), 0) or 0) * 300)
    reclaim = sum(
        (_as_float(row.get("close")) or -np.inf) > (_as_float(row.get(f"ma_{window}")) or np.inf)
        for window in (20, 50)
    ) / 2 * 100
    momentum_reversal = clamp(
        (75 if (_as_float(row.get("rsi_14"), 50) or 50) >= 50 else 25)
        + (15 if (_as_float(row.get("rsi_change_5d"), 0) or 0) > 0 else 0)
        + (10 if (_as_float(row.get("macd_hist"), 0) or 0) > 0 else 0)
    )
    confirmed_covering = 100 if si_change is not None and si_change < float(cfg["short_interest_direction_thresholds"]["decrease"]) else 0
    unwind_score = clamp(
        position_score * 0.20
        + confirmed_covering * 0.30
        + (100 - pressure) * 0.15
        + relative_reversal * 0.15
        + reclaim * 0.10
        + momentum_reversal * 0.10
    )
    return {
        "short_position_score": position_score,
        "short_activity_score": activity_score,
        "short_flow_confirmation_score": flow_confidence,
        "funding_short_score": funding_score,
        "funding_short_quality_score": funding_quality_score,
        "unwind_risk_score": unwind_score,
        "flow_confirmation_signal": flow_signal,
        "components": components,
        "funding_quality_components": funding_quality_components,
    }


def classify_short_regime(row: pd.Series, scores: dict) -> tuple[str, float, dict]:
    position = scores["short_position_score"]
    activity = scores["short_activity_score"]
    funding = scores["funding_short_score"]
    unwind = scores["unwind_risk_score"]
    pressure = clamp(row.get("short_pressure_effectiveness"))
    si_change = _as_float(row.get("si_change_1obs"))
    dtc = _as_float(row.get("days_to_cover"), 0) or 0
    relative_1m = _as_float(row.get("rel_return_1m"), 0) or 0
    reasons = {
        **scores["components"],
        "short_position_score": position,
        "short_activity_score": activity,
        "funding_short_score": funding,
        "funding_short_quality_score": scores.get("funding_short_quality_score"),
        "unwind_risk_score": unwind,
        "actual_si_change_1obs": si_change,
        "flow_confirmation_signal": scores["flow_confirmation_signal"],
        "short_interest_is_publication_gated": True,
        "short_pct_float_available": False,
    }
    if funding >= 70 and unwind >= 70 and si_change is not None and si_change < -0.03:
        return "CONFIRMED_FUNDING_SHORT_UNWIND", clamp((funding + unwind) / 2), reasons
    if position >= 65 and activity >= 65 and pressure <= 40 and relative_1m > 0:
        return "SHORT_PRESSURE_FAILURE", clamp((position + activity + (100 - pressure)) / 3), reasons
    if si_change is not None and si_change < -0.03:
        return "SHORT_COVERING", clamp(60 + min(abs(si_change) * 200, 25)), reasons
    if position >= 85 and dtc >= 5 and activity >= 70:
        return "CROWDED_SHORT", clamp((position + activity + min(dtc * 5, 100)) / 3), reasons
    if si_change is not None and si_change > 0.03 and activity >= 70 and pressure >= 60:
        return "NEW_CONVICTION_SHORT", clamp((activity + pressure + min(si_change * 500, 100)) / 3), reasons
    if funding >= 70 and unwind >= 65 and (si_change is None or si_change >= -0.03):
        return "POTENTIAL_FUNDING_SHORT_UNWIND", clamp((funding + unwind) / 2), reasons
    if funding >= 70:
        return "STRUCTURAL_FUNDING_SHORT", funding, reasons
    if position >= 65 and clamp(row.get("si_persistence_12m"), default=0.0) >= 70:
        return "STABLE_STRUCTURAL_SHORT", clamp((position + funding) / 2), reasons
    return "NEUTRAL_INCONCLUSIVE", clamp(100 - abs(funding - 50), default=50), reasons


def score_and_classify(frame: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    cfg = config or load_short_analytics_config()
    rows = []
    for _, row in frame.iterrows():
        output = row.to_dict()
        scores = score_short_row(row, cfg)
        regime, confidence, reasons = classify_short_regime(row, scores)
        evidence_fields = (
            "short_volume_ratio", "casv_20d", "short_interest", "si_slope_12m",
            "days_to_cover", "rel_return_3m", "dollar_adv20", "ma_50", "rsi_14", "macd_hist",
        )
        evidence_count = sum(pd.notna(row.get(field)) for field in evidence_fields)
        evidence_completeness = evidence_count / len(evidence_fields)
        confidence = min(confidence, 40 + evidence_completeness * 60)
        reasons["evidence_completeness"] = evidence_completeness
        reasons["evidence_fields_available"] = evidence_count
        output.update({key: value for key, value in scores.items() if key not in {"components", "funding_quality_components", "flow_confirmation_signal"}})
        output["short_regime"] = regime
        output["regime_confidence"] = confidence
        output["regime_reason_json"] = reasons
        rows.append(output)
    return pd.DataFrame(rows)


def validate_point_in_time(frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    if {"analytics_date", "publication_date"}.issubset(frame.columns):
        invalid = frame[
            pd.to_datetime(frame["publication_date"], errors="coerce")
            > pd.to_datetime(frame["analytics_date"], errors="coerce")
        ]
        if not invalid.empty:
            errors.append(f"{len(invalid):,} rows use short interest before publication")
    return errors
