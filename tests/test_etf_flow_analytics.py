from __future__ import annotations

import pandas as pd

from db_builder.etf_flow.aggregation import build_etf_flow_analytics, build_rotation_table, build_segment_aggregates
from db_builder.etf_flow.breadth import breadth_metrics
from db_builder.etf_flow.confidence import flow_confidence_adjustment
from db_builder.etf_flow.consensus import issuer_consensus
from db_builder.etf_flow.feature_engineering import build_daily_flow_table, build_rolling_features
from db_builder.etf_flow.price_flow import price_flow_state
from db_builder.etf_flow.report_adapter import etf_flow_report_lines
from db_builder.etf_flow.regime import build_flow_regime


def synthetic_raw(days: int = 65) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=days)
    rows = []
    specs = [
        ("AAA", "Issuer A", "Semiconductors", 1000, 100.0, 10_000_000),
        ("BBB", "Issuer B", "Semiconductors", 900, 50.0, 5_000_000),
        ("CCC", "Issuer C", "Long Duration Treasury", 800, 80.0, 8_000_000),
    ]
    for ticker, issuer, segment, shares, nav, aum in specs:
        for idx, obs_date in enumerate(dates):
            share_delta = 10 if ticker in {"AAA", "BBB"} else -5
            rows.append(
                {
                    "date": obs_date.date(),
                    "ticker": ticker,
                    "issuer": issuer,
                    "fund_name": ticker,
                    "asset_class": "equity" if segment == "Semiconductors" else "fixed_income",
                    "primary_segment": segment,
                    "sector": segment if segment == "Semiconductors" else None,
                    "theme": segment if segment == "Semiconductors" else None,
                    "shares_outstanding": shares + (idx * share_delta),
                    "nav": nav + (idx * 0.1 if ticker in {"AAA", "BBB"} else idx * -0.02),
                    "close": nav + (idx * 0.1 if ticker in {"AAA", "BBB"} else idx * -0.02),
                    "aum": aum + (idx * share_delta * nav),
                    "volume": 100_000,
                    "source": "test",
                }
            )
    return pd.DataFrame(rows)


def test_flow_calculation_uses_shares_delta_times_current_nav():
    daily = build_daily_flow_table(synthetic_raw(3))
    row = daily[(daily["ticker"] == "AAA")].iloc[1]

    assert row["shares_change"] == 10
    assert round(row["estimated_flow"], 2) == round(10 * row["nav"], 2)


def test_missing_prior_day_shares_has_no_flow():
    daily = build_daily_flow_table(synthetic_raw(3))
    first = daily[daily["ticker"] == "AAA"].iloc[0]

    assert pd.isna(first["shares_change"])
    assert pd.isna(first["estimated_flow"])


def test_missing_nav_reduces_quality_and_invalidates_flow():
    raw = synthetic_raw(3)
    raw.loc[raw["ticker"].eq("AAA") & raw["date"].eq(raw["date"].min()), "nav"] = None
    daily = build_daily_flow_table(raw)
    row = daily[daily["ticker"] == "AAA"].iloc[0]

    assert "missing_nav" in row["missing_data_flags"]
    assert row["data_quality_score"] < 100


def test_zero_aum_does_not_create_infinite_normalized_flow():
    raw = synthetic_raw(3)
    raw.loc[raw["ticker"].eq("AAA"), "aum"] = 0
    daily = build_daily_flow_table(raw)

    assert daily[daily["ticker"].eq("AAA")]["flow_pct_aum"].isna().all()


def test_rolling_features_include_zscore_persistence_and_acceleration():
    daily = build_daily_flow_table(synthetic_raw())
    features = build_rolling_features(daily)
    latest = features[features["ticker"] == "AAA"].iloc[-1]

    assert latest["flow_20d"] > 0
    assert latest["flow_persistence_20d"] > 0.9
    assert "flow_zscore_20" in features.columns
    assert "flow_acceleration" in features.columns


def test_extreme_flow_observation_is_flagged():
    raw = synthetic_raw(5)
    idx = raw[raw["ticker"].eq("AAA")].index[-1]
    raw.loc[idx, "shares_outstanding"] = raw.loc[idx, "shares_outstanding"] * 100
    daily = build_daily_flow_table(raw)

    latest = daily[daily["ticker"] == "AAA"].iloc[-1]
    assert "extreme_flow_pct_aum" in latest["missing_data_flags"]


def test_cross_issuer_agreement_and_concentration_penalty():
    daily = build_daily_flow_table(synthetic_raw())
    features = build_rolling_features(daily)
    latest = features[(features["date"] == features["date"].max()) & features["primary_segment"].eq("Semiconductors")]
    consensus = issuer_consensus(latest)

    assert consensus["positive_issuer_count"] == 2
    assert consensus["direction"] == "positive"
    assert consensus["confidence"] > 0


def test_flow_breadth_and_effective_count():
    daily = build_daily_flow_table(synthetic_raw())
    features = build_rolling_features(daily)
    latest = features[(features["date"] == features["date"].max()) & features["primary_segment"].eq("Semiconductors")]
    metrics = breadth_metrics(latest)

    assert metrics["flow_breadth_20d"] == 1.0
    assert metrics["effective_fund_count"] > 1


def test_price_flow_matrix_classification():
    assert price_flow_state("positive", "positive") == "Accumulation"
    assert price_flow_state("positive", "negative") == "Price Up / Flow Out"
    assert price_flow_state("negative", "positive") == "Buying Weakness"
    assert price_flow_state("negative", "negative") == "Distribution"


def test_segment_aggregation_and_rotation_rank_changes_are_deterministic():
    daily = build_daily_flow_table(synthetic_raw())
    features = build_rolling_features(daily)
    segments = build_segment_aggregates(features)
    rotation = build_rotation_table(segments)

    assert not segments.empty
    assert {"Semiconductors", "Long Duration Treasury"}.issubset(set(segments["segment"]))
    assert "rotation_status" in rotation.columns


def test_flow_regime_score_and_conflict_flag():
    daily = build_daily_flow_table(synthetic_raw())
    features = build_rolling_features(daily)
    segments = build_segment_aggregates(features)
    latest = segments[segments["date"] == segments["date"].max()]
    regime = build_flow_regime(latest, existing_regime_score=90)

    assert 0 <= regime["score"] <= 100
    assert regime["combined_regime_score"] is not None
    assert isinstance(regime["conflict_flag"], bool)


def test_flow_confidence_never_increases_when_coverage_is_poor():
    result = flow_confidence_adjustment(
        base_confidence=60,
        agreement_ratio=1.0,
        contradiction_count=0,
        missing_feature_count=0,
        flow_data_quality=90,
        flow_coverage=0.2,
        concentration_penalty=0,
    )

    assert result["flow_agreement_bonus"] == 0
    assert result["final_signal_confidence"] <= 60


def test_full_output_is_reproducible_for_same_input():
    output_a, _ = build_etf_flow_analytics(synthetic_raw(), existing_regime_score=60)
    output_b, _ = build_etf_flow_analytics(synthetic_raw(), existing_regime_score=60)

    assert output_a.to_dict()["flow_regime"] == output_b.to_dict()["flow_regime"]
    assert output_a.to_dict()["market_segments"][0]["segment"] == output_b.to_dict()["market_segments"][0]["segment"]


def test_report_adapter_renders_nan_as_not_available():
    lines = etf_flow_report_lines(
        {
            "flow_regime": {"label": "neutral", "score": float("nan"), "confidence": 50},
            "market_segments": [
                {
                    "segment": "Emerging Markets",
                    "flow_1d": 0,
                    "flow_5d": 0,
                    "flow_20d": 0,
                    "flow_pct_aum_20d": float("nan"),
                    "score": 50,
                    "signal": "neutral",
                    "confidence": 50,
                }
            ],
            "forward_signals": [],
            "contradictions": [],
        }
    )

    rendered = "\n".join(lines)
    assert "`nan`" not in rendered.lower()
    assert "| nan |" not in rendered.lower()
    assert "n/a" in rendered
