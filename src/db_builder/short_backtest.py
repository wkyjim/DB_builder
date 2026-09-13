"""Walk-forward diagnostics for FINRA daily activity and short-interest changes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


FEATURES = [
    "mean_svr",
    "mean_abnormal_svr",
    "interval_casv",
    "fraction_above_75p",
    "fraction_above_90p",
    "svr_acceleration",
]


@dataclass(frozen=True)
class DirectionMetrics:
    threshold: float
    sample_size: int
    hit_rate: float | None
    increase_precision: float | None
    increase_recall: float | None


def _direction(values: pd.Series, threshold: float) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return pd.Series(np.where(numeric > threshold, "INCREASE", np.where(numeric < -threshold, "DECREASE", "FLAT")), index=values.index)


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def direction_metrics(frame: pd.DataFrame, threshold: float) -> DirectionMetrics:
    valid = frame.dropna(subset=["actual_si_change", "expected_si_direction"])
    if valid.empty:
        return DirectionMetrics(threshold, 0, None, None, None)
    actual = _direction(valid["actual_si_change"], threshold)
    predicted = valid["expected_si_direction"].astype(str)
    true_increase = actual.eq("INCREASE")
    predicted_increase = predicted.eq("INCREASE")
    return DirectionMetrics(
        threshold=threshold,
        sample_size=len(valid),
        hit_rate=float(predicted.eq(actual).mean()),
        increase_precision=_safe_ratio(int((true_increase & predicted_increase).sum()), int(predicted_increase.sum())),
        increase_recall=_safe_ratio(int((true_increase & predicted_increase).sum()), int(true_increase.sum())),
    )


def evaluate_interval_predictiveness(intervals: pd.DataFrame, *, train_fraction: float = 0.7) -> dict:
    if intervals.empty:
        return {"sample_size": 0, "message": "No interval observations available."}
    frame = intervals.copy()
    frame["end_publication_date"] = pd.to_datetime(frame["end_publication_date"], errors="coerce")
    frame["actual_si_change"] = pd.to_numeric(frame["actual_si_change"], errors="coerce")
    frame = frame.dropna(subset=["end_publication_date", "actual_si_change"]).sort_values("end_publication_date")
    if frame.empty:
        return {"sample_size": 0, "message": "No published labeled intervals available."}
    split_index = max(1, min(len(frame) - 1, int(len(frame) * train_fraction))) if len(frame) > 1 else len(frame)
    train = frame.iloc[:split_index]
    test = frame.iloc[split_index:]
    correlations = {}
    for feature in FEATURES:
        if feature not in train:
            continue
        subset = train[[feature, "actual_si_change"]].apply(pd.to_numeric, errors="coerce").dropna()
        correlations[feature] = None if len(subset) < 20 else float(subset.corr().iloc[0, 1])
    metrics = [direction_metrics(test if not test.empty else train, threshold).__dict__ for threshold in (0.02, 0.03, 0.05)]
    return {
        "sample_size": len(frame),
        "train_size": len(train),
        "test_size": len(test),
        "split_publication_date": None if test.empty else test["end_publication_date"].min().date().isoformat(),
        "feature_correlations_train": correlations,
        "direction_metrics_test": metrics,
        "publication_date_ordered": True,
        "predictive_claim": "none; diagnostics require economic and out-of-sample review",
    }


def conditional_si_change_table(intervals: pd.DataFrame) -> list[dict]:
    if intervals.empty:
        return []
    frame = intervals.copy()
    frame["activity_bucket"] = pd.cut(
        pd.to_numeric(frame["fraction_above_75p"], errors="coerce"),
        bins=[-np.inf, 0.25, 0.50, 0.75, np.inf],
        labels=["low", "normal", "high", "persistent_high"],
    )
    grouped = frame.groupby("activity_bucket", observed=True)["actual_si_change"]
    return [
        {
            "activity_bucket": str(bucket),
            "sample_size": int(values.notna().sum()),
            "mean_next_si_change": None if values.notna().sum() == 0 else float(pd.to_numeric(values, errors="coerce").mean()),
            "median_next_si_change": None if values.notna().sum() == 0 else float(pd.to_numeric(values, errors="coerce").median()),
        }
        for bucket, values in grouped
    ]
