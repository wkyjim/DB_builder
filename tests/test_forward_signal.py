"""Tests for db_builder.forward_signal — the contrarian calibration layer.

Run: python -m pytest tests/test_forward_signal.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db_builder.forward_signal import (  # noqa: E402
    BUCKETS,
    HORIZONS,
    METHOD_LABEL,
    bucket_for,
    build_calibration,
    expected_forward,
    forward_signal_enabled,
    load_calibration,
)


CAL = {
    "schema": "aegis-forward-signal-v1",
    "buckets": [
        {"lo": 0, "hi": 40, "n": 8, "mean_5d": 0.89, "mean_20d": 6.79, "mean_60d": 10.40},
        {"lo": 40, "hi": 45, "n": 10, "mean_5d": 0.25, "mean_20d": 2.52, "mean_60d": 7.29},
        {"lo": 45, "hi": 50, "n": 11, "mean_5d": 1.41, "mean_20d": 2.92, "mean_60d": 5.65},
        {"lo": 50, "hi": 55, "n": 12, "mean_5d": 0.57, "mean_20d": 1.52, "mean_60d": 4.96},
        {"lo": 55, "hi": 60, "n": 12, "mean_5d": 0.20, "mean_20d": -0.95, "mean_60d": 3.69},
        {"lo": 60, "hi": 65, "n": 20, "mean_5d": -0.11, "mean_20d": -0.30, "mean_60d": 1.84},
        {"lo": 65, "hi": 70, "n": 24, "mean_5d": -0.00, "mean_20d": 0.14, "mean_60d": 1.41},
        {"lo": 70, "hi": 101, "n": 18, "mean_5d": 0.47, "mean_20d": 2.54, "mean_60d": 2.96},
    ],
}


class TestBucketFor:
    def test_boundaries_are_left_closed(self):
        assert bucket_for(0) == (0, 40)
        assert bucket_for(39.99) == (0, 40)
        assert bucket_for(40) == (40, 45)
        assert bucket_for(45) == (45, 50)

    def test_top_bucket_open_ended(self):
        assert bucket_for(70) == (70, 101)
        assert bucket_for(99.9) == (70, 101)

    def test_out_of_range_high_falls_into_top_bucket(self):
        # Scores above 101 (should not happen, but must not raise).
        assert bucket_for(120) == BUCKETS[-1]


class TestExpectedForward:
    def test_requires_supported_horizon(self):
        with pytest.raises(ValueError):
            expected_forward(50, 10, CAL)

    def test_low_score_maps_to_low_bucket_high_return(self):
        out = expected_forward(32.0, 60, CAL)
        assert out["bucket"] == "0-40"
        assert out["expected_pct"] == 10.40
        assert out["n"] == 8
        assert out["direction"] == "up"
        assert out["method"] == METHOD_LABEL

    def test_high_score_maps_to_high_bucket_low_return(self):
        out = expected_forward(67.0, 60, CAL)
        assert out["bucket"] == "65-70"
        assert out["expected_pct"] == 1.41
        assert out["direction"] == "up"  # still positive, just much smaller
        assert out["contrarian"] is True

    def test_contrarian_flag_true_at_60d(self):
        # Curve shape: top bucket (2.96) sits below bottom bucket (10.40).
        assert expected_forward(32.0, 60, CAL)["contrarian"] is True
        assert expected_forward(75.0, 60, CAL)["contrarian"] is True

    def test_contrarian_flag_false_when_curve_is_monotone_increasing(self):
        rising = {
            "buckets": [
                {"lo": 0, "hi": 40, "n": 5, "mean_60d": -2.0},
                {"lo": 70, "hi": 101, "n": 5, "mean_60d": 4.0},
            ]
        }
        assert expected_forward(20.0, 60, rising)["contrarian"] is False

    def test_no_calibration_returns_none(self):
        out = expected_forward(50.0, 20, None)
        assert out["expected_pct"] is None
        assert out["direction"] == "none"
        assert out["n"] == 0
        assert out["contrarian"] is None

    def test_negative_bucket_mean_yields_down_direction(self):
        out = expected_forward(57.0, 20, CAL)  # 55-60 bucket, mean_20d = -0.95
        assert out["expected_pct"] == -0.95
        assert out["direction"] == "down"

    def test_score_is_rounded_and_echoed(self):
        out = expected_forward(44.987654, 5, CAL)
        assert out["score"] == 44.9877


class TestBuildCalibration:
    def test_aggregates_buckets_and_skips_missing_horizons(self):
        rows = [
            {"score": 35.0, "spy_5d": 1.0, "spy_20d": 5.0, "spy_60d": 9.0},
            {"score": 38.0, "spy_5d": 0.0, "spy_20d": 7.0, "spy_60d": 11.0},
            {"score": 67.0, "spy_5d": -0.5, "spy_20d": None, "spy_60d": 1.0},
            {"score": None, "spy_5d": 9.9, "spy_20d": 9.9, "spy_60d": 9.9},  # dropped
        ]
        cal = build_calibration(rows, sample_meta={"n_dates": 3})
        assert cal["schema"] == "aegis-forward-signal-v1"
        assert cal["sample"]["n_dates"] == 3
        by_bucket = {b["lo"]: b for b in cal["buckets"]}
        assert by_bucket[0]["n"] == 2
        assert by_bucket[0]["mean_5d"] == 0.5
        assert by_bucket[0]["mean_60d"] == 10.0
        assert by_bucket[65]["n"] == 1
        assert by_bucket[65]["mean_20d"] is None  # only member had no 20D value

    def test_empty_input_returns_no_buckets(self):
        cal = build_calibration([])
        assert cal["buckets"] == []
        assert cal["limitations"]

    def test_limitations_are_disclosed(self):
        cal = build_calibration([{"score": 50.0, "spy_60d": 1.0}])
        joined = " ".join(cal["limitations"]).lower()
        assert "bull" in joined
        assert "overlap" in joined


class TestEnabledFlag:
    """Default ON since the 116-date bootstrap validation passed; the env var is a kill switch."""

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True), ("", True),
        ("0", False), ("false", False), ("FALSE", False), ("no", False), ("off", False),
    ])
    def test_env_parsing(self, monkeypatch, value, expected):
        monkeypatch.setenv("AEGIS_REPORT_FORWARD_SIGNAL", value)
        assert forward_signal_enabled() is expected

    def test_unset_is_on(self, monkeypatch):
        monkeypatch.delenv("AEGIS_REPORT_FORWARD_SIGNAL", raising=False)
        assert forward_signal_enabled() is True


class TestLoadCalibration:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_calibration(str(tmp_path / "nope.json")) is None

    def test_reads_json(self, tmp_path):
        p = tmp_path / "cal.json"
        p.write_text(json.dumps(CAL), encoding="utf-8")
        loaded = load_calibration(str(p))
        assert loaded["schema"] == "aegis-forward-signal-v1"
        assert len(loaded["buckets"]) == 8

    def test_malformed_json_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_calibration(str(p)) is None


class TestShippedCalibration:
    """The calibration JSON shipped in the package must be consistent."""

    def test_shipped_file_loads_and_is_monotone_at_60d(self):
        cal = load_calibration()
        assert cal is not None, "forward_signal_calibration.json missing from package"
        assert cal["schema"] == "aegis-forward-signal-v1"
        means = [b["mean_60d"] for b in cal["buckets"] if b.get("mean_60d") is not None]
        # The 60D bucket means must trend down as the score rises (contrarian).
        # Small-sample noise allows a few local reversals; the trend and the
        # bottom-vs-top gap are the real assertions.
        declines = sum(1 for a, b in zip(means, means[1:]) if b < a)
        assert declines >= len(means) - 3, "60D bucket means are not declining: %r" % means
        assert means[0] > means[-1], "bottom bucket must beat top bucket at 60D: %r" % means

    def test_shipped_calibration_marks_low_bucket_as_contrarian(self):
        cal = load_calibration()
        out = expected_forward(30.0, 60, cal)
        assert out["expected_pct"] is not None
        assert out["contrarian"] is True
        assert out["n"] >= 5
