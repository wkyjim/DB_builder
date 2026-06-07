from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.sector_rotation import build_sector_rotation_signals, classify_allocation, rotation_score


RUN_TIME = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)


def signal(sector, regime="bull", phase="mid_bull", relative=70, momentum=68, trend=66, news=60, risk=25):
    return {
        "sector_name": sector,
        "related_etfs": ["TEST"],
        "sector_regime": regime,
        "cycle_phase": phase,
        "relative_strength_score": relative,
        "momentum_score": momentum,
        "trend_score": trend,
        "news_score": news,
        "risk_score": risk,
    }


def test_rotation_score_formula_uses_risk_penalty():
    high_risk = signal("Energy", risk=90)
    low_risk = signal("Energy", risk=10)

    assert rotation_score(low_risk) > rotation_score(high_risk)


def test_overweight_classification():
    row = signal("Technology", regime="strong_bull")
    row["rotation_score"] = rotation_score(row)

    assert classify_allocation(row, 1, 8) == ("overweight", "add")


def test_avoid_classification():
    row = signal("Real Estate", regime="strong_bear", risk=90)
    row["rotation_score"] = rotation_score(row)

    assert classify_allocation(row, 8, 8) == ("avoid", "avoid")


def test_relative_strength_ranking():
    df = pd.DataFrame(
        [
            signal("Technology", relative=80),
            signal("Utilities", relative=45),
            signal("Energy", relative=60),
        ]
    )

    signals = build_sector_rotation_signals(df, window_hours=24, run_time=RUN_TIME)
    tech = next(row for row in signals if row["sector_name"] == "Technology")

    assert tech["relative_strength_rank"] == 1
    assert signals[0]["rotation_rank"] == 1


def test_missing_data_neutral():
    df = pd.DataFrame([{"sector_name": "Unknown", "related_etfs": []}])

    signals = build_sector_rotation_signals(df, window_hours=24, run_time=RUN_TIME)

    assert signals[0]["rotation_score"] == 43.0
    assert signals[0]["allocation_bias"] == "neutral"
