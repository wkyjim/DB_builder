"""Configuration helpers for FINRA short analytics."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


DEFAULT_CONFIG = {
    "history": {
        "daily_normalization_observations": 252,
        "daily_baseline_observations": 60,
        "minimum_daily_history": 40,
        "short_interest_observations_per_year": 24,
    },
    "short_interest_direction_thresholds": {"increase": 0.03, "decrease": -0.03},
    "activity": {"high_percentile": 0.8, "extreme_percentile": 0.9, "minimum_total_volume": 1},
    "persistence": {"industry_percentile_threshold": 0.7},
    "quality": {
        "minimum_daily_file_rows": 1000,
        "maximum_short_volume_ratio": 1.05,
        "split_change_threshold": 1.0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_short_analytics_config(path: str | Path | None = None) -> dict:
    config_path = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "finra_short_analytics.json"
    if not config_path.exists():
        return deepcopy(DEFAULT_CONFIG)
    return _merge(DEFAULT_CONFIG, json.loads(config_path.read_text(encoding="utf-8")))
