from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from db_builder.etf_flow.aggregation import build_etf_flow_analytics, build_exposure_aggregates, build_rotation_table, build_segment_aggregates
from db_builder.etf_flow.breadth import breadth_metrics
from db_builder.etf_flow.confidence import flow_confidence_adjustment
from db_builder.etf_flow.consensus import issuer_consensus
from db_builder.etf_flow.feature_engineering import build_daily_flow_table, build_rolling_features
from db_builder.etf_flow.price_flow import price_flow_state
from db_builder.etf_flow.report_adapter import etf_flow_report_lines
from db_builder.etf_flow.regime import build_flow_regime
from db_builder.etf_flow.representative import (
    build_divergence_flags,
    build_etf_flow_signal_daily,
    build_market_flow,
)


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


def synthetic_exposure_raw(days: int = 65) -> pd.DataFrame:
    raw = synthetic_raw(days)
    raw.loc[raw["ticker"].isin(["AAA", "BBB"]), "primary_segment"] = "Semiconductors"
    raw.loc[raw["ticker"].isin(["AAA", "BBB"]), "loaded_at"] = pd.Timestamp("2026-04-15T10:00:00Z")
    return raw


def synthetic_representative_raw(days: int = 90) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=days)
    specs = [
        ("IVV", "BlackRock / iShares", "Broad Equity", 1_000_000, 100.0, 5000),
        ("SPY", "State Street / SPDR", "Broad Equity", 900_000, 100.0, 6000),
        ("IWM", "BlackRock / iShares", "Small Caps", 800_000, 90.0, 7000),
        ("HYG", "BlackRock / iShares", "High Yield Credit", 700_000, 80.0, 8000),
        ("JNK", "State Street / SPDR", "High Yield Credit", 600_000, 80.0, 5000),
        ("TLT", "BlackRock / iShares", "Long Duration Treasury", 500_000, 95.0, 4000),
        ("GLD", "State Street / SPDR", "Gold", 400_000, 180.0, 3000),
        ("IBIT", "BlackRock / iShares", "Bitcoin", 300_000, 60.0, 9000),
        ("XLF", "State Street / SPDR", "Financials", 700_000, 50.0, 5000),
        ("IAT", "BlackRock / iShares", "Regional Banks", 200_000, 40.0, 2000),
    ]
    rows = []
    for ticker, issuer, segment, shares, nav, volume in specs:
        for idx, obs_date in enumerate(dates):
            if ticker in {"SPY", "JNK", "IAT"}:
                delta = -50 if idx >= 60 else 5
            else:
                delta = 100 if idx >= 60 else 5
            current_shares = shares + idx * delta
            current_nav = nav * (1 + idx * 0.0005)
            rows.append(
                {
                    "date": obs_date.date(),
                    "ticker": ticker,
                    "issuer": issuer,
                    "fund_name": ticker,
                    "asset_class": "equity",
                    "primary_segment": segment,
                    "shares_outstanding": current_shares,
                    "nav": current_nav,
                    "close": current_nav,
                    "aum": current_shares * current_nav,
                    "volume": volume + idx * 10,
                    "source": "test issuer",
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


def test_grouped_exposure_aggregates_same_economic_exposure():
    daily = build_daily_flow_table(synthetic_exposure_raw())
    features = build_rolling_features(daily)
    features["flow_momentum"] = features["flow_ema_5"] - features["flow_ema_20"]
    exposures, _ = build_exposure_aggregates(features, analysis_timestamp=datetime(2026, 4, 15, 12, tzinfo=timezone.utc))
    latest = exposures[exposures["date"] == exposures["date"].max()]
    semis = latest[latest["exposure_id"].eq("US_SEMICONDUCTORS")].iloc[0]

    assert semis["reported_etf_count"] == 2
    assert semis["reported_issuer_count"] == 2
    assert semis["known_flow_1d"] > 0
    assert 0 <= semis["adjusted_flow_score"] <= 100


def test_missing_not_yet_released_issuer_is_not_zero_flow():
    raw = synthetic_exposure_raw()
    raw.loc[raw["ticker"].eq("BBB"), "loaded_at"] = pd.Timestamp("2026-04-16T10:00:00Z")
    daily = build_daily_flow_table(raw)
    features = build_rolling_features(daily)
    features["flow_momentum"] = features["flow_ema_5"] - features["flow_ema_20"]
    exposures, availability = build_exposure_aggregates(features, analysis_timestamp=datetime(2026, 4, 15, 12, tzinfo=timezone.utc))
    latest = exposures[exposures["date"] == exposures["date"].max()]
    semis = latest[latest["exposure_id"].eq("US_SEMICONDUCTORS")].iloc[0]

    assert semis["reported_issuer_count"] == 1
    assert semis["eligible_issuer_count"] == 2
    assert semis["data_availability_status"].startswith("provisional")
    assert semis["signal_reliability"] <= 65
    assert "NOT_YET_RELEASED" in set(availability["availability_status"])


def test_low_reliability_score_is_pulled_toward_neutral():
    raw = synthetic_exposure_raw()
    raw.loc[raw["ticker"].eq("BBB"), "loaded_at"] = pd.Timestamp("2026-04-16T10:00:00Z")
    daily = build_daily_flow_table(raw)
    features = build_rolling_features(daily)
    features["flow_momentum"] = features["flow_ema_5"] - features["flow_ema_20"]
    exposures, _ = build_exposure_aggregates(features, analysis_timestamp=datetime(2026, 4, 15, 12, tzinfo=timezone.utc))
    semis = exposures[exposures["exposure_id"].eq("US_SEMICONDUCTORS")].iloc[-1]

    assert abs(semis["adjusted_flow_score"] - 50) < abs(semis["raw_flow_score"] - 50)


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


def test_report_adapter_omits_etf_breadth_language():
    lines = etf_flow_report_lines(
        {
            "flow_regime": {"label": "neutral", "score": 50, "confidence": 60},
            "exposures": [
                {
                    "exposure_name": "Semiconductors",
                    "exposure_type": "sector",
                    "known_flow_1d": 100,
                    "known_flow_5d": 200,
                    "known_flow_20d": 300,
                    "adjusted_flow_score": 65,
                    "signal_reliability": 70,
                    "data_availability_status": "complete",
                }
            ],
            "contradictions": [],
        }
    )

    rendered = "\n".join(lines).lower()
    assert "breadth" not in rendered
    assert "concentration" not in rendered


def test_representative_signal_calculates_required_horizons_and_volume():
    raw = synthetic_representative_raw()
    daily = build_daily_flow_table(raw)
    signals = build_etf_flow_signal_daily(daily, raw)
    latest = signals[signals["ticker"].eq("IVV")].iloc[-1]

    assert latest["flow_1d"] > 0
    assert latest["flow_5d"] > 0
    assert latest["flow_20d"] > 0
    assert latest["flow_60d"] > 0
    assert "flow_zscore_20d" in signals.columns
    assert "volume_zscore_60d" in signals.columns
    assert latest["flow_persistence_20d"] > 0


def test_representative_price_flow_volume_state_matrix():
    raw = synthetic_representative_raw()
    daily = build_daily_flow_table(raw)
    signals = build_etf_flow_signal_daily(daily, raw)
    latest = signals[signals["ticker"].eq("IVV")].iloc[-1]

    assert latest["price_flow_volume_state"] in {
        "Confirmed Accumulation",
        "Steady Sponsorship",
        "Price Leadership",
        "Quiet Accumulation",
        "Neutral",
        "High Turnover Consolidation",
    }
    assert latest["interpretation"]
    assert latest["regime_bias"]
    assert latest["flow_structure"]
    assert "flow_rotation_state" not in signals.columns


def test_market_flow_score_uses_representative_tickers():
    raw = synthetic_representative_raw()
    daily = build_daily_flow_table(raw)
    signals = build_etf_flow_signal_daily(daily, raw)
    market = build_market_flow(signals)

    assert 0 <= market["market_flow_score"] <= 100
    assert market["market_flow_regime"] in {
        "Strong Broad Risk-On",
        "Moderate Risk-On",
        "Selective Risk-On",
        "Mixed / Neutral",
        "Defensive Rotation",
        "Moderate Risk-Off",
        "Strong Risk-Off",
    }
    assert market["equity_risk_flow_score"] != 50


def test_close_substitute_and_subsector_divergence_flags():
    raw = synthetic_representative_raw()
    daily = build_daily_flow_table(raw)
    signals = build_etf_flow_signal_daily(daily, raw)
    flags = build_divergence_flags(signals)

    assert not flags.empty
    assert {"close_substitute_divergence", "related_subsector_divergence"} & set(flags["flag_type"])


def test_representative_report_renders_required_sections():
    raw = synthetic_representative_raw()
    daily = build_daily_flow_table(raw)
    signals = build_etf_flow_signal_daily(daily, raw)
    market = build_market_flow(signals)
    latest = signals[signals["date"].eq(signals["date"].max())].to_dict(orient="records")
    rendered = "\n".join(
        etf_flow_report_lines(
            {
                "market_flow": market,
                "representative_signals": latest,
                "representative_divergences": build_divergence_flags(signals).to_dict(orient="records"),
            }
        )
    )

    assert "ETF Flows Analysis" in rendered
    assert "Core Flow Signals" in rendered
    assert "Sector Flow Signals" in rendered
    assert "Subsector PFV Signals" in rendered
    assert "Representative Exposure Dashboard" not in rendered
    assert "Rotation State" not in rendered
    assert "Flow Structure" in rendered



